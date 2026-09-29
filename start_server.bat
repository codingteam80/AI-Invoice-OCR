@echo off
REM Start AI Invoice OCR in host-PC mode. Run this ON THE HOST PC only.
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (call ".venv\Scripts\activate.bat") else if exist "venv\Scripts\activate.bat" (call "venv\Scripts\activate.bat")

echo.
echo ==============================================================
echo  AI Invoice OCR is starting. Other PCs can open ONE of these:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo    http:%%a:8501
echo  On THIS PC use: http://localhost:8501
echo  IGNORE any 'http://0.0.0.0:8501' shown below - that is NOT a browsable address.
echo  (Keep this window open. Closing it stops the app for everyone.)
echo ==============================================================
echo.

REM app.py opens http://localhost:8501 in your browser automatically once ready.
python app.py ui
pause
