#!/bin/sh
# Make the Pi's WiFi robust. Run once with sudo (install.sh does it for you):
#   sudo ./tools/setup-network.sh
# Safe to run again.
set -e
if [ "$(id -u)" != 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== WiFi power saving off (the usual cause of Pi WiFi dropping out)"
cat > /etc/NetworkManager/conf.d/crt-tv-wifi-powersave-off.conf <<'EOF'
[connection]
# 2 = disable
wifi.powersave = 2
EOF

echo "== Keep retrying the WiFi connection forever"
nmcli -t -f NAME,TYPE connection show | awk -F: '$2 == "802-11-wireless" {print $1}' |
    while read -r name; do
        nmcli connection modify "$name" connection.autoconnect-retries 0
        echo "   $name"
    done

echo "== Keep logs across reboots (so we can see why it dropped next time)"
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald

echo "== WiFi watchdog (checks the router every 2 minutes)"
install -m 755 "$DIR/wifi-watchdog.sh" /usr/local/sbin/crt-tv-wifi-watchdog
cat > /etc/systemd/system/crt-tv-wifi-watchdog.service <<'EOF'
[Unit]
Description=Reconnect WiFi if the router can't be reached

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/crt-tv-wifi-watchdog
EOF
cat > /etc/systemd/system/crt-tv-wifi-watchdog.timer <<'EOF'
[Unit]
Description=Check the network every 2 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now crt-tv-wifi-watchdog.timer

systemctl reload NetworkManager
echo "== Done. Power saving switches off at the next reboot or reconnect."
echo "   Watchdog log: journalctl -t wifi-watchdog"
