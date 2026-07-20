@echo off
title XAUUSD ICT Bot
cd /d %~dp0

:inicio
echo.
echo ============================================
echo   XAUUSD BOT - %date% %time%
echo ============================================
python bot.py
echo.
echo Bot detenido. Reiniciando en 15 segundos...
timeout /t 15 /nobreak
goto inicio
