@echo off
setlocal

cd /d "%~dp0"
title Deadlock OTP Telegram/Twitch Giveaway Bot

if not exist ".env" (
    echo Не найден файл .env рядом с start_bot.cmd
    echo.
    echo Создайте .env из .env.example и заполните токены/ID.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Не найден Python виртуального окружения: .venv\Scripts\python.exe
    echo.
    echo Сначала установите зависимости:
    echo py -m venv .venv
    echo .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Запускаю Telegram/Twitch giveaway bot...
echo Папка: %CD%
echo.

".venv\Scripts\python.exe" -m app

echo.
echo Бот остановлен. Если выше есть ошибка, пришлите её мне.
pause
