source /etc/ajib/core/scripts/path.sh || true 

echo "Uninstalling ajib..."

SERVICES=(
    "ajib-server.service"
    "ajib-telegram-bot.service"
)

echo "Running uninstallation script..."
bash <(curl -fsSL https://get.hy2.sh/) --remove >/dev/null 2>&1

echo "Removing ajib folder..."
rm -rf /etc/ajib >/dev/null 2>&1

echo "Deleting ajib user..."
userdel -r ajib >/dev/null 2>&1 || true 

echo "Stop/Disabling ajib Services..."
for service in "${SERVICES[@]}" "ajib-server@*.service"; do
    echo "Stopping and disabling $service..."
    systemctl stop "$service" > /dev/null 2>&1 || true  
    systemctl disable "$service" > /dev/null 2>&1 || true 
done

echo "Removing systemd service files..."
for service in "${SERVICES[@]}" "ajib-server@*.service"; do
    echo "Removing service file: $service"
    rm -f "/etc/systemd/system/$service" "/etc/systemd/system/multi-user.target.wants/$service" >/dev/null 2>&1
done

echo "Reloading systemd daemon..."
systemctl daemon-reload >/dev/null 2>&1

echo "Removing cron jobs..."
if crontab -l 2>/dev/null | grep -q "ajib"; then 
    (crontab -l | grep -v "ajib" | crontab -) >/dev/null 2>&1
fi

echo "Removing alias 'ajib' from .bashrc..."
sed -i '/alias ajib=.*\/etc\/ajib\/menu.sh/d' ~/.bashrc 2>/dev/null || true 

echo "ajib uninstalled!"
echo ""
