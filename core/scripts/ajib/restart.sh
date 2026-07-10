#!/bin/bash

python3 /etc/ajib/core/cli.py traffic-status > /dev/null 2>&1
if systemctl restart ajib-server.service; then
    echo "ajib server restarted successfully."
else
    echo "Error: Failed to restart the ajib server."
fi