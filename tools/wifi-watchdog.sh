#!/bin/sh
# Bring the network back when the Pi can't reach its router, escalating step by step.
# Run every 2 minutes as root by crt-tv-wifi-watchdog.timer (installed by setup-network.sh).
# Logs to the journal: journalctl -t wifi-watchdog
STATE=/run/crt-tv-wifi-failures

gateway=$(ip route show default | awk '{print $3; exit}')
if [ -n "$gateway" ] && ping -c 2 -W 3 "$gateway" >/dev/null 2>&1; then
    [ -f "$STATE" ] && logger -t wifi-watchdog "network is back" && rm -f "$STATE"
    exit 0
fi

failures=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$failures" > "$STATE"
wifi=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2 == "802-11-wireless" {print $1; exit}')
logger -t wifi-watchdog "can't reach the router (${gateway:-no route}), attempt $failures"

case "$failures" in
    1 | 2) nmcli connection up "$wifi" ;;
    3)     nmcli radio wifi off; sleep 5; nmcli radio wifi on ;;
    4)     systemctl restart NetworkManager ;;
    5)     modprobe -r brcmfmac && sleep 2 && modprobe brcmfmac ;;  # the WiFi chip's driver hung
    *)
        # Last resort, but never within 30 minutes of booting so a dead router can't
        # cause a reboot loop.
        uptime_s=$(cut -d. -f1 /proc/uptime)
        if [ "$failures" -ge 8 ] && [ "$uptime_s" -gt 1800 ]; then
            logger -t wifi-watchdog "still no network, rebooting"
            systemctl reboot
        fi
        ;;
esac
