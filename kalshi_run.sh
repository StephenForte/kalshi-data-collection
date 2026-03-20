#!/bin/bash
# kalshi_run.sh
# Wrapper that activates the venv and runs the monitor.
# Place this in ~/kalshi-monitor/

cd ~/kalshi-monitor
source venv/bin/activate
python kalshi_monitor.py >> logs/kalshi_monitor.log 2>&1
