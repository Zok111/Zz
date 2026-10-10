# PRISM AI: Telegram Mini App for AI market analysis

You open the bot in Telegram and tap **Open App**. Then you pick an expiration and tap **GET SIGNAL**.

The backend then:
- scans every live currency pair and ranks them by setup quality;
- sends the top candidates to **Gemini** and shows you the selected pair;
- runs a 20-second countdown, then reveals **UP / DOWN** with a chart and a dashed **AI Scenario**;
- records **WIN / LOSS / DRAW** automatically from the quote stream.

**It never trades.** No orders, no account control. You decide and act manually.

## Run on Windows

1. Install **Python 3.12** (python.org; tick *Add python.exe to PATH*) and **Git**.
2. Get the code:
   ```powershell
   git clone https://github.com/zok111/zz.git
   cd zz
   git checkout phase1-market-data
   ```
3. Double-click **`setup.bat`**. It creates `.venv`, installs dependencies, creates `.env` and
   installs `cloudflared`, the free HTTPS tunnel that Telegram requires.
4. Open `.env` in Notepad and fill in `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL` and `PO_SSID`.
5. Double-click **`start.bat`**, or run `python -m app.main` with the venv active.
6. In Telegram, open your bot, send `/start` and tap **Open App** (or use the menu button next to the input field).

The tunnel address changes every time you start. The bot updates its button automatically.

### Getting the values

- **TELEGRAM_BOT_TOKEN:** in Telegram talk to @BotFather → `/newbot` → copy the token.
- **GEMINI_API_KEY:** https://aistudio.google.com/apikey
- **GEMINI_MODEL:** a current model id listed in Google AI Studio, e.g. `gemini-2.5-flash`. At
  startup the app checks that the model exists and logs `ai_ready` or `ai_check_failed`.
- **PO_SSID** (use a **demo** account):
  1. Log in to Pocket Option in Chrome and switch to demo.
  2. Press F12 → Network → WS, then reload the page.
  3. Click the WebSocket connection → Messages, and copy the sent message starting with `42["auth",`.
  4. Paste it on one line, unquoted. It expires with the session; repeat if market data stops authenticating.
- **ALLOWED_TELEGRAM_USER_IDS** (recommended): your numeric Telegram ID. It's shown in the app's Profile tab.

## About the data source

Pocket Option has no official API. This app uses the unofficial, reverse-engineered
`BinaryOptionsToolsV2` library, **read-only**.

- The library is reachable only through a whitelist proxy, and tests make sure trading and
  account methods can't be called.
- By default only demo sessions are accepted.
- The library may break without notice and may conflict with the broker's terms.

If market data or AI is unavailable, the app says so (**MARKET DATA UNAVAILABLE**,
**AI UNAVAILABLE**). It never substitutes fake data.

## How results are determined

All timestamps are set by the server.

| Field | Rule |
|---|---|
| `entry_at` | the moment UP/DOWN is revealed |
| Entry price | last recorded tick at or before `entry_at` |
| Exit price | last recorded tick at or before `expires_at` |
| Missing tick | if either tick is older than 10 s, the signal is VOID (never guessed) |

- The direction is fixed before the pair is shown and revealed later, so it can't change with hindsight.
- Your broker entry happens a few seconds after ours, so your real result can differ.
- Every request is stored in `data/signals.db`, including NO_TRADE, cancelled and failed ones. Each
  record keeps the exact market snapshot sent to Gemini and its raw response.

## Project layout

```
app/main.py              launcher (tunnel + server + bot)
app/server.py            FastAPI REST + WebSocket, serves the Mini App
app/signals.py           signal state machine and settlement
app/analysis/            features, scanner ranking, Gemini analyzer
app/marketdata/          provider interface, candle engine, hub, Pocket Option adapter
app/telegram_bot.py      bot: only provides the "Open App" button
app/telegram_auth.py     Telegram initData signature check
app/store.py             SQLite storage and statistics
webapp/                  the Mini App (HTML/CSS/JS, no build step)
tests/                   pytest; tests/fakes.py holds test doubles used only by tests
app/probe.py             market-data diagnostic: python -m app.probe
```

Run the tests with `pytest -q`.
