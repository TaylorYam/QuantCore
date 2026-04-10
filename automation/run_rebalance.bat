@echo off
cd /d %~dp0
echo [%date% %time%] Starting QuantCore rebalance (both)...
python rebalance_ib.py --mode both --account all
echo [%date% %time%] Finished.
