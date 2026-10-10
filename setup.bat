@echo off
REM One-time setup: virtual environment, dependencies, .env file.
cd /d "%~dp0"
where py >nul 2>nul || (echo Python launcher "py" not found. Install Python 3.12 from python.org and tick "Add to PATH". & pause & exit /b 1)
if not exist .venv (
  py -3.12 -m venv .venv || (echo Could not create the virtual environment with Python 3.12. & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (echo Dependency installation failed. & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
where cloudflared >nul 2>nul || if not exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
  echo.
  echo Installing cloudflared ^(free HTTPS tunnel for the Mini App^)...
  winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
)
echo.
echo Setup done. Now open .env in Notepad, fill in the 4 REQUIRED values, then run start.bat
pause
