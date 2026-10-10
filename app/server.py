"""FastAPI backend: REST + WebSocket for the Telegram Mini App, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analysis.features import MIN_CANDLES
from app.analysis.gemini import GeminiAnalyzer
from app.config import EXPIRATIONS, WEBAPP_DIR, Settings
from app.logs import kv
from app.marketdata.base import MarketDataProvider
from app.marketdata.hub import MarketHub
from app.signals import Busy, RateLimited, SignalEngine, now_ms
from app.store import Store, compute_stats
from app.telegram_auth import AuthError, validate_init_data

log = logging.getLogger("server")


class Broadcaster:
    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        msg = {**msg, "server_now": now_ms()}
        for q in list(self.clients):
            if q.qsize() < 500:
                q.put_nowait(msg)


class SignalRequest(BaseModel):
    expiration_seconds: int


def create_app(settings: Settings, provider: MarketDataProvider, bot=None, analyzer=None) -> FastAPI:
    store = Store(settings.db_path)
    hub = MarketHub(provider, settings)
    analyzer = analyzer or GeminiAnalyzer(settings)
    bus = Broadcaster()
    engine = SignalEngine(hub, analyzer, store, settings, bus.publish)
    version = str(int(time.time()))
    tasks: list[asyncio.Task] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        tasks.append(asyncio.create_task(analyzer.check()))
        tasks.append(asyncio.create_task(status_loop()))
        if bot is not None:
            tasks.append(asyncio.create_task(bot.run()))
        log.info(kv("server_started", port=settings.port))
        yield
        await engine.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot is not None:
            await bot.close()
        await hub.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub, app.state.engine, app.state.store = hub, engine, store

    # ------------------------------------------------------------------ helpers
    def status() -> dict:
        return {
            "market": hub.status(),
            "ai": analyzer.status(),
            "bot": {"state": bot.state if bot else "disabled", "username": bot.username if bot else None},
            "server": "ok",
        }

    async def status_loop():
        while True:
            await asyncio.sleep(5)
            bus.publish({"type": "status", "status": status()})

    def authenticate(init_data: str) -> dict:
        token = settings.telegram_bot_token
        if init_data and token:
            try:
                user = validate_init_data(init_data, token.get_secret_value())
            except AuthError as e:
                raise HTTPException(401, str(e)) from e
        elif settings.dev_allow_browser:
            user = {"id": 0, "first_name": "Local", "username": "browser"}
        else:
            raise HTTPException(401, "Open this app from the Telegram bot")
        allowed = settings.allowed_user_ids
        if allowed and user["id"] not in allowed and user["id"] != 0:
            raise HTTPException(403, f"Access restricted (your Telegram ID: {user['id']})")
        return user

    def current_user(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        return authenticate(header[4:] if header.lower().startswith("tma ") else "")

    # ------------------------------------------------------------------ REST
    @app.get("/api/bootstrap")
    def bootstrap(user: dict = Depends(current_user)):
        return {
            "app_name": settings.app_name,
            "user": user,
            "expirations": EXPIRATIONS,
            "countdown_seconds": settings.signal_countdown_seconds,
            "candle_interval_seconds": 60,
            "ai_candles": settings.ai_candles,
            "min_candles": MIN_CANDLES,
            "status": status(),
            "signal": engine.public(engine.current),
            "server_now": now_ms(),
        }

    @app.post("/api/signal")
    async def request_signal(body: SignalRequest, user: dict = Depends(current_user)):
        try:
            sig = await engine.request(int(user["id"]), body.expiration_seconds)
        except Busy:
            raise HTTPException(409, "A signal is already in progress")
        except RateLimited:
            raise HTTPException(429, "Please wait a few seconds before requesting again")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"signal": sig, "server_now": now_ms()}

    @app.get("/api/signal/current")
    def current_signal(user: dict = Depends(current_user)):
        return {"signal": engine.public(engine.current), "server_now": now_ms()}

    @app.get("/api/history")
    def history(limit: int = 50, before: int | None = None, user: dict = Depends(current_user)):
        rows = store.history(min(max(limit, 1), 200), before)
        return {"items": [engine.public(r) for r in rows], "server_now": now_ms()}

    @app.get("/api/stats")
    def stats(user: dict = Depends(current_user)):
        return {"stats": compute_stats(store.settled(), store.status_counts()), "status": status(),
                "versions": {"model": analyzer.model, "prompt": engine.current and engine.current.get("prompt_version"),
                             "strategy": engine.current and engine.current.get("strategy_version")},
                "server_now": now_ms()}

    @app.get("/api/time")
    def server_time():
        return {"server_now": now_ms()}

    # ------------------------------------------------------------------ WebSocket
    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        try:
            authenticate(websocket.query_params.get("auth", ""))
        except HTTPException as e:
            await websocket.close(code=4401, reason=str(e.detail)[:120])
            return
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        bus.clients.add(q)
        log.info(kv("ws_connected", clients=len(bus.clients)))
        try:
            await websocket.send_json({"type": "hello", "signal": engine.public(engine.current),
                                       "status": status(), "server_now": now_ms()})

            async def pump():
                while True:
                    await websocket.send_json(await q.get())

            async def drain():
                while True:
                    await websocket.receive_text()

            done, pending = await asyncio.wait({asyncio.create_task(pump()), asyncio.create_task(drain())},
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.clients.discard(q)
            log.info(kv("ws_disconnected", clients=len(bus.clients)))

    # ------------------------------------------------------------------ static Mini App
    index_html = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = index_html.replace("__V__", version).replace("__APP_NAME__", settings.app_name)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

    @app.exception_handler(HTTPException)
    async def http_error(_req, exc: HTTPException):
        return JSONResponse({"detail": exc.detail, "server_now": now_ms()}, status_code=exc.status_code)

    return app
