@echo off
REM Coleta Inteligente - modo OFFLINE (sem internet), banco local coleta_local.db
REM Duplo clique para iniciar. Para parar, feche a janela ou aperte Ctrl+C.
cd /d "%~dp0"
chcp 65001 >nul
python gerenciar.py iniciar --offline
pause
