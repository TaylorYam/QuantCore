@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore SP500 rebalance (Wednesday)...
python rebalance_ib.py --mode sp500 --account all
echo [%date% %time%] Finished.
