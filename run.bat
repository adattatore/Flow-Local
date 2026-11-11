@echo off
title FlowLocal - AI Voice Typing (Whisper + Qwen 2.5)
cd /d "%~dp0"

:: Avvia Ollama in background se non è già attivo
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe">NUL
if "%ERRORLEVEL%"=="1" (
    echo Avvio del servizio Ollama in background...
    start "" /B ollama serve
    timeout /t 2 /nobreak >NUL
)

echo Avvio di FlowLocal su GPU NVIDIA RTX 5060...
uv run python main.py
pause
