@echo off
REM ============================================================
REM  Gunluk otomasyon: tum kanallar icin video uret + yukle.
REM  Windows Task Scheduler bunu her gun belirli saatte calistirir.
REM  ONKOSUL: start_all.bat ile servisler acik olmali.
REM ============================================================
cd /d "%~dp0"
python daily.py >> daily.log 2>&1
