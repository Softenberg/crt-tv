# CRT TV

A Raspberry Pi "TV" that plays random movies and shows from a Jellyfin server on genre
channels, with a CRT shader, channel-change static and the occasional sync glitch.

## Disclaimer 
All Code written with AI. mainly made this for personal use for fun so don't expect too much

## Hardware

- Raspberry Pi 4
- Official Raspberry Pi 7" touch display (800×480, DSI)
- KY-040 style rotary encoder with push button
- MAX98357A I2S amp + 4 Ω speaker (or powered speakers on the 3.5mm jack)

### Wiring

| Rotary encoder | Pi |
|---|---|
| CLK | GPIO17 (pin 11) |
| DT | GPIO27 (pin 13) |
| SW | GPIO22 (pin 15) |
| + | 3.3V (pin 1), **not 5V** |
| GND | GND (pin 9) |

| MAX98357A | Pi |
|---|---|
| VIN | 5V (pin 2) |
| GND | GND (pin 6) |
| BCLK | GPIO18 (pin 12) |
| LRC | GPIO19 (pin 35) |
| DIN | GPIO21 (pin 40) |

## Install on a fresh Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its settings, set the
   username, Wi-Fi and SSH, then boot the Pi and SSH in.
2. Clone and run the installer:
   ```sh
   sudo apt-get install -y git
   git clone https://github.com/<you>/crt-tv.git ~/crt-tv
   cd ~/crt-tv && ./install.sh
   ```
   It installs the packages, sets up audio, asks for the Jellyfin server and API key
   (Jellyfin: Dashboard → API Keys), and installs the `crt-tv` service that starts at boot.

If the amp is silent, check the card name with `aplay -l` and set `audio_device` in
`config.json` to match (e.g. `alsa/plughw:CARD=MAX98357A`).

## Controls

| | Action |
|---|---|
| Turn knob | Change channel |
| Click knob | Skip to another programme |
| Hold knob 1–3 s, release | Back to the start of the programme |
| Hold knob 3 s | Settings menu |
| Tap screen | Show what's on + control bar (CH ▼/▲, START, SKIP, VOL −/+, MENU) |
| Swipe up / down | Change channel |
| Swipe left / right | Skip |
| Long-press screen | Settings menu |

Settings menu: tune-in mode (random point in the first 20 min / from the beginning / live
schedule), movies and/or shows, anime included / excluded / only, subtitles (on / auto for
non-English audio / off), picture (stretch or keep aspect ratio, CRT effect off / light /
medium / heavy, glitches), volume, sleep timer, which genre channels are on, and show channels.

**Show channels**: pick series under *Show channels* and each gets its own channel after the
genre channels. It plays the show in order (specials and OP/ED extras skipped, wrapping round
after the last episode) and resumes where you left off. SKIP goes to the next episode, START
restarts the current one. Progress is kept in `settings.json` and never sent to Jellyfin.

**Audio and subtitles**: Japanese audio is preferred, then English, then the file's default.
Subtitles are English (full dialogue rather than signs/songs or forced tracks). Jellyfin passes
the track through in the stream and mpv draws it under the CRT shader. (Burning subtitles in
on the server makes Jellyfin extract the whole track first, which took 20–50 s per programme.)

**Loading**: between programmes the TV shows an SVT-style PM5544 test card with a live clock,
glitching (if glitches are on). `tv.py` fetches the stream itself and only switches once data
is flowing, so the card covers however long Jellyfin takes and the cut costs a frame or two.
The card is drawn by `tools/make_testcard.py` (needs Pillow) into `assets/testcard.png`.

## Files

| | |
|---|---|
| `tv.py` | Everything: Jellyfin client, channel logic, mpv control, encoder/touch input, menus |
| `shaders/crt-*.glsl` | CRT presets. The tuning values are the `#define`s at the top of each file |
| `shaders/glitch.glsl` | The sync glitch, switched on briefly every 4–10 minutes and over the test card |
| `assets/testcard.png` | The loading screen, drawn by `tools/make_testcard.py` |
| `config.json` | Per-device: server, API key, audio device (created by `install.sh`, not in git) |
| `settings.json` | Menu choices, saved automatically (not in git) |

Other `config.json` options (defaults in `DEFAULT_CONFIG` in `tv.py`): `user` (Jellyfin user,
empty = first admin), `max_width`/`max_height`/`video_bitrate` (stream quality),
`min_channel_items`, `random_start_max`, `card_min_seconds`, `glitch_every`, `glitch_length`,
`encoder` pins and `"reverse": true` if the knob turns the wrong way, and `touch`
`swap_xy`/`invert_x`/`invert_y` if taps land in the wrong place.

## Network

`install.sh` runs `tools/setup-network.sh`, which turns off WiFi power saving (the usual
cause of Pi WiFi dropping out), makes NetworkManager retry forever, keeps logs across reboots,
and installs a watchdog that pings the router every 2 minutes. If the router can't be reached
it escalates: reconnect, toggle WiFi, restart NetworkManager, reload the WiFi driver, and as a
last resort reboot (only after ~16 minutes without network, and never within 30 minutes of
booting). See what it did with `journalctl -t wifi-watchdog`.

## Updating and debugging

```sh
cd ~/crt-tv && git pull && sudo systemctl restart crt-tv
journalctl -u crt-tv -f                  # logs, including every input
echo up > /tmp/crt-tv.cmd                # remote control for testing:
# up | down | click | hold | menu | restart | glitch | tap X Y | swipe up|down|left|right | quit
```

`quit` exits; systemd restarts it after 3 seconds, which also reloads the code.
