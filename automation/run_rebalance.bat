@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore rebalance (both)...
python rebalance_ib.py --mode both
echo [%date% %time%] Finished.
