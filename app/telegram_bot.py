"""Minimal Telegram bot: its only job is to give you the button that opens the Mini App."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import Settings
from app.logs import kv

log = logging.getLogger("bot")


class MiniAppBot:
    def __init__(self, settings: Settings, webapp_url: str):
        self.settings = settings
        self.url = webapp_url
        self.username: str | None = None
        self.state = "stopped"
        self.error: str | None = None
        token = settings.telegram_bot_token.get_secret_value()
        self._api = f"https://api.telegram.org/bot{token}/"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))

    async def _call(self, method: str, **params):
        r = await self._http.post(self._api + method, json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description', r.status_code)}")
        return data["result"]

    def _open_button(self) -> dict:
        return {"inline_keyboard": [[{"text": f"Open {self.settings.app_name}", "web_app": {"url": self.url}}]]}

    async def setup(self) -> None:
        me = await self._call("getMe")
        self.username = me.get("username")
        await self._call("deleteWebhook", drop_pending_updates=False)
        await self._call("setMyCommands", commands=[{"command": "start", "description": "Open the app"}])
        await self._call("setChatMenuButton", menu_button={
            "type": "web_app", "text": "Open App", "web_app": {"url": self.url}})
        log.info(kv("bot_ready", username=self.username, webapp=self.url))

    async def run(self) -> None:
        backoff = 2.0
        while True:
            try:
                await self.setup()
                self.state, self.error = "running", None
                offset = 0
                while True:
                    updates = await self._call("getUpdates", offset=offset, timeout=30,
                                               allowed_updates=["message"])
                    for u in updates:
                        offset = u["update_id"] + 1
                        await self._handle(u.get("message") or {})
                    backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state, self.error = "error", str(e)[:200]
                log.warning(kv("bot_error", error=type(e).__name__, detail=str(e)[:200]))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle(self, msg: dict) -> None:
        chat = msg.get("chat", {}).get("id")
        user = msg.get("from", {})
        if not chat or not (msg.get("text") or "").startswith("/start"):
            return
        allowed = self.settings.allowed_user_ids
        if allowed and user.get("id") not in allowed:
            await self._call("sendMessage", chat_id=chat,
                             text=f"Access is restricted. Your Telegram ID is {user.get('id')}.")
            return
        await self._call("sendMessage", chat_id=chat, reply_markup=self._open_button(),
                         text=f"{self.settings.app_name}: AI market analysis.\n"
                              f"Tap the button to open the app.")

    async def close(self) -> None:
        await self._http.aclose()
