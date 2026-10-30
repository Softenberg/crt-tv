# CRT TV

A Raspberry Pi "TV" that plays random movies and shows from a Jellyfin server on genre
channels, with a CRT shader, channel-change static and the occasional sync glitch.

## Disclaimer 
All Code written with AI. mainly made this for personal use for fun so don't expect too much

## Hardware

- Raspberry Pi 4
- Official Raspberry Pi 7" touch display (800×480, DSI)
- Two KY-040 style rotary encoders with push buttons (channels; volume/brightness)
- MAX98357A I2S amp + 4 Ω speaker (or powered speakers on the 3.5mm jack)

### Wiring

| Encoder | Channel knob | Second knob (volume/brightness) |
|---|---|---|
| CLK | GPIO17 (pin 11) | GPIO23 (pin 16) |
| DT | GPIO27 (pin 13) | GPIO24 (pin 18) |
| SW | GPIO22 (pin 15) | GPIO25 (pin 22) |
| + | 3.3V (pin 1), **not 5V** | 3.3V (pin 17), **not 5V** |
| GND | GND (pin 9) | GND (pin 20) |

If a knob turns the wrong way, set `"reverse": true` under `encoder` / `volume_encoder` in
`config.json`.

| MAX98357A | Pi |
|---|---|
| VIN | 5V (pin 2) |
| GND | GND (pin 6) |
| BCLK | GPIO18 (pin 12) |
| LRC | GPIO19 (pin 35) |
| DIN | GPIO21 (pin 40) |

## Install

You need the Pi and display above (the knobs and amp are optional), a Jellyfin server, and
an API key for it: in Jellyfin, **Dashboard → API Keys → +**.

1. Flash **Raspberry Pi OS Lite (64-bit)** to an SD card with
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In its OS customisation, set a
   username and password, your Wi-Fi, and turn on SSH.
2. Boot the Pi, SSH in (`ssh <username>@<pi-address>`) and run:
   ```sh
   sudo apt-get update && sudo apt-get install -y git
   git clone https://github.com/Softenberg/crt-tv.git ~/crt-tv
   cd ~/crt-tv && ./install.sh
   ```
3. Answer the installer's questions: whether you use the MAX98357A amp, your Jellyfin server
   address (e.g. `http://192.168.1.20:8096`), the API key, and optionally a YouTube playlist.

The installer checks that it can reach Jellyfin, then sets the TV up to start at every boot
(rebooting once first if it enabled the amp). Settings are in `~/crt-tv/config.json`; after
changing it, run `sudo systemctl restart crt-tv`.

**Not working?** `journalctl -u crt-tv -f` shows the log. No sound from the amp: find the card
name with `aplay -l` and set `audio_device` in `config.json` to match (e.g.
`alsa/plughw:CARD=MAX98357A`).

## Controls

| | Action |
|---|---|
| Turn channel knob | Change channel |
| Click channel knob | Skip to another programme |
| Hold channel knob 1–3 s, release | Back to the start of the programme |
| Hold channel knob 3 s | Settings menu |
| Turn second knob | Volume (`volume_step`, default 2% per click) |
| Press second knob | Switch it to screen brightness (`brightness_step`, default 1%); back to volume after 5 idle seconds |
| Tap screen | Show what's on + control bar (CH ▼/▲, START, SKIP, VOL −/+, MENU) |
| Swipe up / down | Change channel |
| Swipe left / right | Skip |
| Long-press screen | Settings menu |

Settings menu: tune-in mode (random point in the first 20 min / from the beginning / live
schedule), movies and/or shows, anime included / excluded / only, subtitles (on / auto for
non-English audio / off), picture (stretch or keep aspect ratio, CRT effect off / light /
medium / heavy, glitches, brightness, night dimming), volume, sleep timer, which genre channels are on, and show channels.

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
`min_channel_items`, `random_start_max`, `card_min_seconds`, `night_hours` and
`night_brightness` (night dimming, default 22-07 at 20%), `glitch_every`, `glitch_length`,
`encoder`/`volume_encoder` pins and `"reverse": true` if a knob turns the wrong way, and `touch`
`swap_xy`/`invert_x`/`invert_y` if taps land in the wrong place.

## YouTube channel

Give the installer a playlist URL (or set `youtube_playlist` in `config.json`) and the TV
downloads it in the background (2 minutes after starting, then every 6 hours) with
`tools/youtube-sync.py`, into `youtube/`. Once the first video is in, a **YouTube** channel
appears last in the channel list.

- Up to 480p, preferring H.264. Files are named `<title> [<video id>].mkv`, so nothing is
  downloaded twice.
- Newest first: with `youtube_newest` `"last"` (YouTube's default, new videos go at the end
  of the playlist) or `"first"`. When `youtube_max_gb` (default 8) is used up or less than
  `youtube_min_free_gb` (default 3) would be left free, the oldest videos are deleted to make
  room for newer ones.
- Downloads are capped at `youtube_rate_limit` (default `3M`) so playback doesn't stutter.
- A current yt-dlp lives in `.venv` (install.sh sets it up; the sync updates it weekly) and
  uses Node.js, which YouTube now requires. Run a sync by hand: `python3 tools/youtube-sync.py`.

**Without the server**: if Jellyfin can't be reached, at boot or later, the TV switches to
the YouTube channel (marked SERVER OFFLINE under the title) and tries the server again after
1, 5 and 15 minutes, then every 30 minutes. When it answers, the other channels come back and
the TV returns to the channel it was on. With no videos downloaded it shows NO SIGNAL and
retries every 10 seconds. A programme that stalls for 20 seconds waiting for data is dropped
rather than left as a frozen picture; on a show channel it resumes where it was.

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
# up | down | click | hold | menu | restart | glitch | vol+ | vol- | knob | tap X Y | swipe DIR | quit
```

`quit` exits; systemd restarts it after 3 seconds, which also reloads the code.
