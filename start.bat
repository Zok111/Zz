@echo off
REM Starts backend + market data + Gemini analyzer + Telegram bot + HTTPS tunnel.
cd /d "%~dp0"
if not exist .venv (echo Run setup.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m app.main
pause
