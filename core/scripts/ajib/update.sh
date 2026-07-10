#!/bin/bash

# Source the path.sh script to load the CONFIG_FILE variable
source /etc/ajib/core/scripts/path.sh

echo "Starting the update process for ajib..."
echo "Backing up the current configuration..."
cp "$CONFIG_FILE" /etc/ajib/config_backup.json
if [ $? -ne 0 ]; then
    echo "Error: Failed to back up configuration. Aborting update."
    exit 1
fi

echo "Downloading and installing the latest version of ajib..."
bash <(curl -fsSL https://get.hy2.sh/) >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "Error: Failed to download or install the latest version. Restoring backup configuration."
    mv /etc/ajib/config_backup.json "$CONFIG_FILE"
    python3 "$CLI_PATH" restart-ajib > /dev/null 2>&1
    exit 1
fi

echo "Restoring configuration from backup..."
mv /etc/ajib/config_backup.json "$CONFIG_FILE"
if [ $? -ne 0 ]; then
    echo "Error: Failed to restore configuration from backup."
    exit 1
fi

rm /etc/ajib/config.yaml
systemctl daemon-reload >/dev/null 2>&1
python3 "$CLI_PATH" restart-ajib > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "Error: Failed to restart ajib service."
    exit 1
fi

echo "ajib has been successfully updated."
echo ""
exit 0
