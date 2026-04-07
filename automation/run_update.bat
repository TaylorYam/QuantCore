@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore data update...
python update_data.py
echo [%date% %time%] Finished.
