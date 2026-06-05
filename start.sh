#!/bin/bash
# Watchdog script for AegisQuant

echo "Starting AegisQuant Watchdog..."
echo "Press [CTRL+C] to stop."

while true
do
    echo "----------------------------------------"
    echo "Starting Trading Bot..."
    echo "----------------------------------------"
    
    python3 Main.py
    
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -ne 0 ]; then
        echo "Bot crashed with exit code $EXIT_CODE. Restarting in 5 seconds..."
        sleep 5
    else
        echo "Bot stopped cleanly."
        break
    fi
done
