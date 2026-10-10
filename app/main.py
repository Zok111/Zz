"""Start everything:  python -m app.main

1. Gets an HTTPS URL for the Mini App (PUBLIC_URL from .env, or a free cloudflared quick tunnel).
2. Starts the backend (market data, scanner, Gemini, signals, API, Mini App files).
3. Starts the Telegram bot, which sets the "Open App" button to that URL.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time

import uvicorn

from app.config import get_settings
from app.logs import kv, setup_logging
from app.marketdata.pocketoption import PocketOptionProvider
from app.server import create_app
from app.telegram_bot import MiniAppBot

log = logging.getLogger("main")

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_PATHS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
]


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    return next((p for p in CLOUDFLARED_PATHS if os.path.exists(p)), None)


def start_tunnel(port: int) -> tuple[subprocess.Popen, str] | None:
    exe = find_cloudflared()
    if not exe:
        return None
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    url: list[str] = []
    found = threading.Event()

    def reader():
        for line in proc.stdout:
            if not url:
                m = TUNNEL_RE.search(line)
                if m:
                    url.append(m.group(0))
                    found.set()

    threading.Thread(target=reader, daemon=True).start()
    if not found.wait(45):
        proc.terminate()
        raise SystemExit("cloudflared started but did not report a tunnel URL within 45 s")
    time.sleep(3)  # give the new hostname a moment to become reachable
    return proc, url[0]


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    tunnel = None
    url = settings.public_url.strip().rstrip("/")
    if not url:
        started = start_tunnel(settings.port)
        if started:
            tunnel, url = started
            log.info(kv("tunnel_ready", url=url))
        else:
            log.warning(kv("no_https_url", hint="install cloudflared or set PUBLIC_URL; "
                                                "Telegram requires HTTPS for Mini Apps"))

    bot = None
    if settings.telegram_bot_token and url.startswith("https://"):
        bot = MiniAppBot(settings, url + "/")
    elif not settings.telegram_bot_token:
        log.warning(kv("bot_disabled", reason="TELEGRAM_BOT_TOKEN is empty"))

    app = create_app(settings, PocketOptionProvider(settings), bot)
    print(f"\n  Mini App URL : {url or '(none)'}\n  Local        : http://127.0.0.1:{settings.port}\n"
          f"  Open your bot in Telegram and press 'Open App'.  Ctrl+C to stop.\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="warning")
    finally:
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    main()
