@echo off
REM Coleta Inteligente - roda no computador usando o banco online (Neon).
REM Precisa de internet e do arquivo .env.local (criado com "vercel env pull").
REM Na feira, com internet, o mais simples é usar o site publicado no Vercel.
cd /d "%~dp0"
chcp 65001 >nul
python gerenciar.py iniciar
pause
