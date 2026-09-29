@echo off
REM ONE-TIME setup on the host PC: allow other PCs to reach port 8501.
REM Right-click this file -> "Run as administrator".
netsh advfirewall firewall add rule name="AI Invoice OCR (8501)" dir=in action=allow protocol=TCP localport=8501 profile=private,domain
echo.
echo Done. If it says "Ok." the port is open for Private/Domain networks.
pause
