@echo off
REM ============================================================
REM  Starts the four core micro-services (+ optional Piper TTS).
REM  Each service opens in its own window. Requires ffmpeg on PATH.
REM ============================================================
cd /d "%~dp0"

echo [1/5] Edge-TTS (5002)
start "Edge-TTS - 5002" cmd /k "python edge_tts_server.py"

echo [2/5] Piper TTS (5000, offline fallback - optional)
start "Piper TTS - 5000" cmd /k "python piper_server.py"

echo [3/5] Subtitle server / Whisper alignment (5060)
start "Subtitle Server - 5060" cmd /k "python subtitle_server.py"

echo [4/5] Photo / clip server (5080)
start "Photo Server - 5080" cmd /k "python photo_server.py"

echo [5/5] FFmpeg render server (5050)
start "FFmpeg Server - 5050" cmd /k "python ffmpeg_server.py"

echo.
echo All services started. Whisper needs ~10-30 s to load.
echo Health check:  python saglik.py
echo.
pause
