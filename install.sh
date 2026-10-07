#!/bin/sh
# Install CRT TV on Raspberry Pi OS Lite (Bookworm/Trixie).
# Run from the cloned repo as the user that should run the TV (not root):
#   ./install.sh
# Safe to run again: it only adds what's missing.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
CONFIG="$DIR/config.json"
BOOT_CONFIG=/boot/firmware/config.txt
REBOOT=0

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root (it uses sudo where needed)." >&2
    exit 1
fi

echo "== Installing packages"
sudo apt-get update
sudo apt-get install -y mpv python3-evdev python3-gpiozero python3-lgpio python3-requests fonts-dejavu-core

echo "== Giving $USER_NAME access to display, touch, GPIO and audio"
sudo usermod -aG video,render,input,gpio,audio "$USER_NAME"

sudo "$DIR/tools/setup-network.sh"

echo "== Audio output"
printf "Using a MAX98357A I2S amp? [y/N] "
read -r amp
case "$amp" in
    [yY]*)
        AUDIO="alsa/plughw:CARD=MAX98357A"
        if ! grep -q "^dtoverlay=max98357a" "$BOOT_CONFIG"; then
            printf '\n[all]\ndtoverlay=max98357a\n' | sudo tee -a "$BOOT_CONFIG" >/dev/null
            REBOOT=1
        fi
        ;;
    *)
        AUDIO="alsa/plughw:CARD=Headphones"  # the 3.5mm jack
        ;;
esac

if [ -f "$CONFIG" ]; then
    echo "== Keeping existing config.json (audio set to $AUDIO)"
    python3 - "$CONFIG" "$AUDIO" <<'EOF'
import json, sys
path, audio = sys.argv[1:]
cfg = json.load(open(path))
cfg["audio_device"] = audio
json.dump(cfg, open(path, "w"), indent=2)
EOF
else
    echo "== Jellyfin connection"
    printf "Jellyfin server [http://192.168.0.100:8096]: "
    read -r server
    printf "Jellyfin API key (Dashboard > API Keys): "
    read -r key
    python3 - "$CONFIG" "${server:-http://192.168.0.100:8096}" "$key" "$AUDIO" <<'EOF'
import json, sys
path, server, key, audio = sys.argv[1:]
json.dump({"server": server, "api_key": key, "user": "", "audio_device": audio},
          open(path, "w"), indent=2)
EOF
    chmod 600 "$CONFIG"
fi

echo "== Installing the crt-tv service"
sudo tee /etc/systemd/system/crt-tv.service >/dev/null <<EOF
[Unit]
Description=CRT TV (Jellyfin random channels)
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
SupplementaryGroups=video render input gpio audio
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/tv.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable crt-tv

if [ "$REBOOT" = 1 ]; then
    echo "== Done. Rebooting to enable the audio amp..."
    sudo reboot
else
    sudo systemctl restart crt-tv
    echo "== Done. CRT TV is starting; logs: journalctl -u crt-tv -f"
fi
