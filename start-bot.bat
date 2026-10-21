@echo off
title Second Brain bot (keep this window open)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
echo Starting your Second Brain bot... Keep this window open. Close it to stop the bot.
".venv\Scripts\python.exe" bot.py
echo.
echo The bot stopped. Check logs\bot.log for the reason.
pause
