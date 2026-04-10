@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore daily report...
python daily_report.py --account all
echo [%date% %time%] Finished.
