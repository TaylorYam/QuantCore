@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore SR rebalance (Tuesday)...
python rebalance_ib.py --mode sr --account all
echo [%date% %time%] Finished.
