@echo off
REM Coleta Inteligente - confere se está tudo pronto (online e offline)
cd /d "%~dp0"
chcp 65001 >nul
echo ===== MODO ONLINE (Neon) =====
python gerenciar.py verificar
echo.
echo ===== MODO OFFLINE (coleta_local.db) =====
python gerenciar.py verificar --offline
pause
