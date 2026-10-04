#!/usr/bin/env python3
"""CRT TV: random Jellyfin movies and shows on genre "channels".

Rotary encoder: turn to change channel, click to skip, hold 1-3 s and release
to rewind to the start, hold 3 s for settings.
Touchscreen: tap for what's on and the control bar, swipe up/down to change
channel, swipe sideways to skip, long-press for settings.
For testing over SSH:
  echo up|down|click|press|release|hold|menu|restart|glitch|tap X Y|swipe DIR|quit > /tmp/crt-tv.cmd
"""

import datetime
import errno
import json
import logging
import math
import os
import queue
import random
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
SETTINGS_PATH = BASE / "settings.json"
SHADER_DIR = BASE / "shaders"
TESTCARD = BASE / "assets" / "testcard.png"  # made by tools/make_testcard.py
CLOCK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FIFO_PATH = "/tmp/crt-tv.cmd"
MPV_SOCK = "/tmp/crt-tv-mpv.sock"

SCREEN_W, SCREEN_H = 800, 480
TICKS_PER_SEC = 10_000_000
FONT = r"\fnDejaVu Sans Mono"
GREEN = "&H66FF33&"  # ASS colours are BGR

log = logging.getLogger("crt-tv")

DEFAULT_CONFIG = {
    "server": "http://192.168.0.100:8096",
    "api_key": "",
    "user": "",  # Jellyfin user whose library is used; empty = first admin
    "audio_device": "alsa/plughw:CARD=Headphones",
    "max_width": 854,
    "max_height": 480,
    "video_bitrate": 2500000,
    "min_channel_items": 3,
    "random_start_max": 1200,  # seconds; tune-in point is within this (and half the runtime)
    "card_min_seconds": 1.0,  # the test card shows at least this long on a channel change
    "glitch_every": [240, 600],  # seconds between random sync glitches (min, max)
    "glitch_length": [0.15, 0.4],
    "encoder": {"clk": 17, "dt": 27, "sw": 22, "reverse": False},
    "touch": {"device": "", "swap_xy": False, "invert_x": False, "invert_y": False},
}

DEFAULT_SETTINGS = {
    "tune_mode": "random",
    "content": "both",
    "anime": "on",
    "aspect": "stretch",
    "crt": "light",
    "glitches": "on",
    "subtitles": "on",
    "volume": 80,
    "disabled_genres": [],
    "show_channels": [],   # series ids with their own channel, in channel order
    "show_progress": {},   # series id -> {"episode": id, "position": seconds}
    "last_channel": None,  # "genre:<name>" or "show:<series id>"
}

TUNE_MODES = [("random", "Random (first 20m)"), ("beginning", "From beginning"), ("live", "Live schedule")]
CONTENT = [("both", "Movies + shows"), ("movies", "Movies only"), ("shows", "Shows only")]
ANIME = [("on", "Included"), ("off", "Excluded"), ("only", "Anime only")]
ASPECT = [("stretch", "Stretch"), ("keep", "Keep ratio")]
CRT_LEVELS = [("off", "Off"), ("light", "Light"), ("medium", "Medium"), ("heavy", "Heavy")]
GLITCHES = [("on", "Sometimes"), ("off", "Off")]
SUBTITLES = [("on", "On"), ("auto", "Auto (foreign)"), ("off", "Off")]
SLEEP_MINUTES = [0, 30, 60, 90]

AUDIO_PREFERENCE = [{"jpn", "ja", "japanese"}, {"eng", "en", "english"}]
ENGLISH = {"eng", "en", "english"}


def load_json(path, defaults):
    data = json.loads(json.dumps(defaults))  # deep copy
    if path.exists():
        data.update(json.loads(path.read_text()))
    return data


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def option_label(options, key):
    return dict(options).get(key, key)


def next_option(options, key):
    keys = [k for k, _ in options]
    return keys[(keys.index(key) + 1) % len(keys)] if key in keys else keys[0]


def ass_escape(text, limit=44):
    text = str(text).replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tidy(name):
    """A release-style name made readable: "[CBM]_Cowboy_Bebop_-_Session_01 (720p)" ->
    "Cowboy Bebop - Session 01"."""
    clean = re.sub(r"\[[^\]]*\]|\([^)]*\)|\.(mkv|mp4|avi)$", " ", name or "").replace("_", " ")
    if " " not in clean.strip() and clean.count(".") >= 2:  # "Twin.Peaks.S01E02.Traces.to.Nowhere"
        clean = clean.replace(".", " ")
    clean = re.sub(r"\s+", " ", clean).strip(" -.")
    return clean or name or ""


def runtime_seconds(item):
    return (item.get("RunTimeTicks") or 0) / TICKS_PER_SEC


def describe(item):
    """Title lines for the on-screen banner."""
    if item.get("Type") == "Episode":
        line = tidy(item.get("SeriesName"))
        if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
            line += f" · S{item['ParentIndexNumber']:02d}E{item['IndexNumber']:02d}"
        elif item.get("_episode_no"):  # show channels know the position even without tags
            line += f" · EP {item['_episode_no']}/{item['_episode_total']}"
        return [line, tidy(item.get("Name"))]
    year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    return [f"{item.get('Name') or ''}{year}"]


def natural_key(text):
    """Sort key that orders "ep 2" before "ep 10"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


EXTRAS = re.compile(r"(?<![a-z])(nc)?(op|ed)\d*(?![a-z])|playall|picture.?drama|preview|menu", re.I)


def file_name(episode):
    """The episode's file name without release-group, resolution and checksum tags."""
    name = (episode.get("Path") or episode.get("Name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)


def file_episode_key(episode):
    """(title, season, episode, name) read from a file name, for releases without usable tags.

    Understands "S02E05", "2x05", "[3 of 26]", and otherwise takes the last plain number
    ("Top o Nerae 2! Diebuster 1" is episode 1). The title part keeps two shows that share
    a folder apart instead of interleaving their episodes.
    """
    raw = (episode.get("Path") or episode.get("Name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = file_name(episode)

    def title(text):
        return re.sub(r"[^a-z]", "", re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text).lower())

    m = re.search(r"s(\d{1,2})\s*e(\d{1,3})(?!\d)|(?<![\da-z])(\d{1,2})x(\d{1,3})(?!\d)", raw, re.I)
    if m:
        season, number = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return "", int(season), int(number), natural_key(name)  # explicit, so naming style can't matter
    m = re.search(r"(?<!\d)(\d{1,3})\s*of\s*\d{1,3}(?!\d)", raw, re.I)
    if m:
        return title(raw[:m.start()]), 1, int(m.group(1)), natural_key(name)
    numbers = list(re.finditer(r"(?<![a-z\d.])(\d{1,3})(?![\d.]|p\b)", name, re.I))
    if numbers:
        return title(name[:numbers[-1].start()]), 1, int(numbers[-1].group(1)), natural_key(name)
    return title(name), 1, 0, natural_key(name)


def episode_order(episodes):
    """Episodes in viewing order, without credit sequences and other extras.

    Season/episode numbers when they're trustworthy (all present, no duplicates); otherwise
    numbers read from the file names, since badly tagged fansub releases still number their files.
    """
    main = [e for e in episodes if not EXTRAS.search(file_name(e)) and not 0 < runtime_seconds(e) < 240]
    episodes = main or episodes  # a "series" that is all extras still gets a channel
    numbers = [(e.get("ParentIndexNumber") or 0, e.get("IndexNumber")) for e in episodes]
    if all(n is not None for _, n in numbers) and len(set(numbers)) == len(numbers):
        return [e for _, e in sorted(zip(numbers, episodes), key=lambda pair: pair[0])]
    return sorted(episodes, key=file_episode_key)


def choose_streams(streams, subtitles):
    """Pick (audio index, subtitle index or None) from an item's MediaStreams.

    Audio: Japanese, then English, then the file's default track. Subtitles, when the
    setting asks for them: English (or untagged), preferring full dialogue over forced or
    signs-and-songs tracks.
    """
    def lang(s):
        return (s.get("Language") or "").lower()

    audio_tracks = [s for s in streams if s.get("Type") == "Audio"]
    audio = next((s for codes in AUDIO_PREFERENCE for s in audio_tracks if lang(s) in codes), None) \
        or next((s for s in audio_tracks if s.get("IsDefault")), None) \
        or (audio_tracks[0] if audio_tracks else None)
    wanted = subtitles == "on" or (subtitles == "auto" and audio is not None and lang(audio) not in ENGLISH)
    subtitle = None
    if wanted:
        def score(s):
            title = (s.get("Title") or s.get("DisplayTitle") or "").lower()
            partial = s.get("IsForced") or any(w in title for w in ("forced", "sign", "song"))
            return lang(s) in ENGLISH, not partial
        candidates = [s for s in streams if s.get("Type") == "Subtitle" and (lang(s) in ENGLISH or not lang(s))]
        subtitle = max(candidates, key=score) if candidates else None
    return (audio["Index"] if audio else None), (subtitle["Index"] if subtitle else None)


def testcard_url():
    """The loading screen: the test card as an endless 25 fps video with a live clock in its
    box, so the CRT and glitch shaders animate over it like over any programme."""
    # The clock box centre is y=390 on the 768x576 card (CLOCK_BOX in make_testcard.py).
    # One backslash before the colon: the graph parser and the option parser each take a level.
    return (f"av://lavfi:movie={TESTCARD},loop=loop=-1:size=1,setpts=N/25/TB,fps=25,format=yuv420p,"
            f"drawtext=fontfile={CLOCK_FONT}:fontsize=28:fontcolor=white:x=(w-tw)/2:y=390-th/2:"
            r"text='%{localtime\:%T}'")


class StreamRelay:
    """Fetches a programme from Jellyfin and hands it to mpv through a FIFO.

    mpv can't tell us when a stream will start, so loading it directly means black screen
    while Jellyfin spins up the transcode. Fetching it here first lets the test card stay up
    until data is actually flowing; switching then costs a frame or two of black.
    """
    READY_BYTES = 192 * 1024

    def __init__(self, url, on_ready, on_fail):
        self.path = f"/tmp/crt-tv-stream-{uuid.uuid4().hex}.mkv"
        os.mkfifo(self.path)
        self.cancelled = False
        threading.Thread(target=self._run, args=(url, on_ready, on_fail), daemon=True).start()

    def _run(self, url, on_ready, on_fail):
        try:
            with requests.get(url, stream=True, timeout=(5, 120)) as r:
                r.raise_for_status()
                chunks = r.iter_content(64 * 1024)
                head = bytearray()
                for chunk in chunks:
                    head += chunk
                    if self.cancelled:
                        return
                    if len(head) >= self.READY_BYTES:
                        break
                on_ready(self)
                out = self._open_for_mpv()
                if out is None:
                    return
                with out:
                    out.write(head)
                    for chunk in chunks:
                        if self.cancelled:
                            return
                        out.write(chunk)
        except BrokenPipeError:
            pass  # mpv moved on to something else
        except (requests.RequestException, OSError) as e:
            if not self.cancelled:
                on_fail(self, e)
        finally:
            self.cancelled = True
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _open_for_mpv(self, timeout=30):
        """The FIFO's write end once mpv has opened it, or None if cancelled meanwhile.
        (A plain blocking open could hang forever if mpv never comes.)"""
        deadline = time.monotonic() + timeout
        while not self.cancelled and time.monotonic() < deadline:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno != errno.ENXIO:  # ENXIO: no reader yet
                    raise
                time.sleep(0.02)
                continue
            os.set_blocking(fd, True)
            return os.fdopen(fd, "wb")
        return None

    def cancel(self):
        """Stop relaying (which also ends Jellyfin's transcode, as the request closes)."""
        self.cancelled = True


# --------------------------------------------------------------------------- Jellyfin

class Jellyfin:
    CACHE_TTL = 3600
    DEVICE_ID = "crt-tv"

    def __init__(self, server, api_key, user=""):
        self.server = server.rstrip("/")
        self.api_key = api_key
        self.http = requests.Session()
        self.http.headers["Authorization"] = (
            f'MediaBrowser Client="CRT TV", Device="Raspberry Pi", DeviceId="{self.DEVICE_ID}", '
            f'Version="1.0", Token="{api_key}"')
        self._cache = {}
        self.user_id = self._resolve_user(user)

    def _get(self, path, **params):
        r = self.http.get(self.server + path, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _resolve_user(self, name):
        users = self._get("/Users")
        if name:
            for u in users:
                if u["Name"].lower() == name.lower():
                    return u["Id"]
            raise LookupError(f"Jellyfin user {name!r} not found")
        admins = [u for u in users if u.get("Policy", {}).get("IsAdministrator")]
        return (admins or users)[0]["Id"]

    def _cached(self, key, fetch):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.CACHE_TTL:
            return hit[1]
        value = fetch()
        self._cache[key] = (time.time(), value)
        return value

    def _items(self, **params):
        return self._get("/Items", userId=self.user_id, Recursive="true", EnableImages="false",
                         EnableUserData="false", ExcludeLocationTypes="Virtual", **params)

    def genres(self, types):
        data = self._get("/Genres", userId=self.user_id, IncludeItemTypes=",".join(types),
                         Recursive="true", SortBy="SortName")
        return [g["Name"] for g in data["Items"]]

    def genre_pool(self, genre, types):
        """Every movie/series in a genre, in a stable order (for the live schedule)."""
        return self._cached(("pool", genre, tuple(types)), lambda: sorted(
            self._items(Genres=genre, IncludeItemTypes=",".join(types))["Items"], key=lambda i: i["Id"]))

    def episodes(self, series_id):
        return self._cached(("episodes", series_id), lambda: sorted(
            self._items(ParentId=series_id, IncludeItemTypes="Episode")["Items"], key=lambda i: i["Id"]))

    def ordered_episodes(self, series_id):
        """A series' episodes in viewing order, without specials (season 0)."""
        def fetch():
            episodes = self._get(f"/Shows/{series_id}/Episodes", userId=self.user_id, IsMissing="false",
                                 Fields="Path", EnableImages="false", EnableUserData="false")["Items"]
            return episode_order([e for e in episodes
                                  if e.get("ParentIndexNumber") != 0 and e.get("LocationType") != "Virtual"])
        return self._cached(("ordered", series_id), fetch)

    def all_series(self):
        """[(id, name)] of every series, by name."""
        return self._cached(("series",), lambda: [
            (i["Id"], i["Name"]) for i in self._items(IncludeItemTypes="Series", SortBy="SortName")["Items"]])

    def media_streams(self, item_id):
        items = self._items(Ids=item_id, Fields="MediaStreams")["Items"]
        return items[0].get("MediaStreams") or [] if items else []

    def anime_ids(self):
        """Movies/series that are anime: everything in a library named "...anime..." plus
        anything tagged "anime" elsewhere."""
        def fetch():
            ids = {i["Id"] for i in self._items(Tags="anime", IncludeItemTypes="Movie,Series")["Items"]}
            for view in self._get(f"/Users/{self.user_id}/Views")["Items"]:
                if "anime" in view["Name"].lower():
                    ids |= {i["Id"] for i in
                            self._items(ParentId=view["Id"], IncludeItemTypes="Movie,Series")["Items"]}
            return ids
        return self._cached(("anime",), fetch)

    def stream_url(self, item, cfg, session, start=0.0, audio=None, subtitle=None):
        # A progressive transcode that starts at the tune-in point: one ffmpeg job per
        # programme and no seeking (mpv's HLS demuxer always fetches segment 0 first,
        # making Jellyfin start and kill a second transcode).
        params = {
            "ApiKey": self.api_key,
            "MediaSourceId": item["Id"],
            "DeviceId": self.DEVICE_ID,
            "PlaySessionId": session,
            "Container": "mkv",
            "StartTimeTicks": int(start * TICKS_PER_SEC),
            "VideoCodec": "h264",
            "AudioCodec": "aac",
            "MaxWidth": cfg["max_width"],
            "MaxHeight": cfg["max_height"],
            "VideoBitrate": cfg["video_bitrate"],
            "AudioBitrate": 160000,
            "MaxAudioChannels": 2,
            "TranscodingMaxAudioChannels": 2,
        }
        if audio is not None:
            params["AudioStreamIndex"] = audio
        if subtitle is not None:
            # Passed through in the MKV for mpv to draw. Burning them in ("Encode") makes
            # Jellyfin extract the whole subtitle track first: 20-50 s before the first frame.
            params.update(SubtitleStreamIndex=subtitle, SubtitleMethod="Embed")
        return f"{self.server}/Videos/{item['Id']}/stream.mkv?{urlencode(params)}"

    def stop_transcode(self, session):
        def stop():
            try:
                self.http.delete(self.server + "/Videos/ActiveEncodings", timeout=5,
                                 params={"deviceId": self.DEVICE_ID, "playSessionId": session})
            except requests.RequestException:
                pass
        threading.Thread(target=stop, daemon=True).start()


# --------------------------------------------------------------------------- mpv

class Mpv:
    """mpv subprocess controlled over its JSON IPC socket."""

    def __init__(self, args, on_event):
        if os.path.exists(MPV_SOCK):
            os.unlink(MPV_SOCK)
        self.on_event = on_event
        self.proc = subprocess.Popen(["mpv", *args, f"--input-ipc-server={MPV_SOCK}"],
                                     stdin=subprocess.DEVNULL)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.sock.connect(MPV_SOCK)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("mpv failed to start")
                time.sleep(0.1)
        self.lock = threading.Lock()
        self.next_id = 0
        self.pending = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        while chunk := self.sock.recv(65536):
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in filter(None, lines):
                msg = json.loads(line)
                if "event" in msg:
                    self.on_event(msg)
                elif slot := self.pending.pop(msg.get("request_id"), None):
                    slot[1] = msg
                    slot[0].set()
        self.on_event({"event": "mpv-exit"})

    def _send(self, command, timeout=5, quiet=False):
        with self.lock:
            self.next_id += 1
            slot = [threading.Event(), None]
            self.pending[self.next_id] = slot
            self.sock.sendall(json.dumps({"command": command, "request_id": self.next_id}).encode() + b"\n")
        name = command["name"] if isinstance(command, dict) else command[0]
        if not slot[0].wait(timeout):
            log.warning("mpv: no reply to %s", name)
            return None
        if slot[1].get("error") != "success":
            if not quiet:
                log.warning("mpv: %s failed: %s", name, slot[1].get("error"))
            return None
        return slot[1].get("data")

    def command(self, *args):
        return self._send(list(args))

    def get(self, prop):
        """A property's value, or None if it's unavailable (e.g. nothing playing)."""
        return self._send(["get_property", prop], quiet=True)

    def set(self, prop, value):
        return self.command("set_property", prop, value)

    def osd(self, overlay_id, ass, z=0):
        self._send({"name": "osd-overlay", "id": overlay_id, "format": "ass-events", "data": ass,
                    "res_x": SCREEN_W, "res_y": SCREEN_H, "z": z})

    def osd_clear(self, overlay_id):
        self._send({"name": "osd-overlay", "id": overlay_id, "format": "none", "data": ""})

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------- inputs

def start_encoder(cfg, post):
    try:
        from gpiozero import Button, RotaryEncoder
    except ImportError:
        log.warning("gpiozero not available; rotary encoder disabled")
        return None
    pins = cfg["encoder"]
    sign = -1 if pins.get("reverse") else 1
    try:
        encoder = RotaryEncoder(pins["clk"], pins["dt"], max_steps=0)
        button = Button(pins["sw"], pull_up=True, bounce_time=0.03)
    except Exception as e:  # gpiozero raises a variety of pin/back-end errors
        log.warning("rotary encoder disabled: %s", e)
        return None
    encoder.when_rotated_clockwise = lambda: post("rotate", sign)
    encoder.when_rotated_counter_clockwise = lambda: post("rotate", -sign)
    # Click vs. long press is decided in TV.on_knob_release from how long it was held.
    button.when_pressed = lambda: post("press")
    button.when_released = lambda: post("release")
    return encoder, button  # keep references alive


def touch_loop(cfg, post, long_press=0.8, swipe=80):
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        log.warning("python3-evdev not installed; touchscreen disabled")
        return
    tcfg = cfg["touch"]
    dev = None
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        caps = d.capabilities().get(ecodes.EV_ABS, [])
        codes = {c[0] if isinstance(c, tuple) else c for c in caps}
        if (tcfg["device"] and tcfg["device"] in (path, d.name)) or \
                (not tcfg["device"] and ecodes.ABS_MT_POSITION_X in codes):
            dev = d
            break
    if dev is None:
        log.warning("no touchscreen found")
        return
    log.info("touchscreen: %s (%s)", dev.name, dev.path)
    ax = dev.absinfo(ecodes.ABS_X)
    ay = dev.absinfo(ecodes.ABS_Y)

    def to_screen(rx, ry):
        x = (rx - ax.min) / max(1, ax.max - ax.min)
        y = (ry - ay.min) / max(1, ay.max - ay.min)
        if tcfg["swap_xy"]:
            x, y = y, x
        if tcfg["invert_x"]:
            x = 1 - x
        if tcfg["invert_y"]:
            y = 1 - y
        return int(x * SCREEN_W), int(y * SCREEN_H)

    def release(start, end):
        dx, dy = end[0] - start[0], end[1] - start[1]
        if max(abs(dx), abs(dy)) < swipe:
            post("tap", start)
        elif abs(dy) > abs(dx):
            post("swipe", "up" if dy < 0 else "down")
        else:
            post("swipe", "left" if dx < 0 else "right")

    rx = ry = 0
    touching = was_touching = fired = False
    start = pos = (0, 0)
    down_at = None
    while True:
        ready, _, _ = select.select([dev.fd], [], [], 0.05)
        if ready:
            for e in dev.read():
                if e.type == ecodes.EV_ABS and e.code == ecodes.ABS_X:
                    rx = e.value
                elif e.type == ecodes.EV_ABS and e.code == ecodes.ABS_Y:
                    ry = e.value
                elif e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                    touching = bool(e.value)
                elif e.type == ecodes.EV_SYN and e.code == ecodes.SYN_REPORT:
                    # Act on whole frames: the finger position and touch state arrive in any order.
                    pos = to_screen(rx, ry)
                    if touching and not was_touching:
                        start, down_at, fired = pos, time.monotonic(), False
                    elif was_touching and not touching:
                        if not fired:
                            release(start, pos)
                        down_at = None
                    was_touching = touching
        moved = max(abs(pos[0] - start[0]), abs(pos[1] - start[1]))
        if down_at is not None and not fired and moved < swipe / 2 \
                and time.monotonic() - down_at >= long_press:
            fired = True
            post("longpress")


def fifo_loop(post):
    if os.path.exists(FIFO_PATH) and not stat.S_ISFIFO(os.stat(FIFO_PATH).st_mode):
        os.unlink(FIFO_PATH)
    if not os.path.exists(FIFO_PATH):
        os.mkfifo(FIFO_PATH)
    while True:
        with open(FIFO_PATH) as f:
            for line in f:
                if line.strip():
                    post("cmd", line.strip())


# --------------------------------------------------------------------------- settings menu

class Menu:
    TOP, ROW_H, PER_PAGE = 66, 58, 5

    TITLES = {"main": "SETTINGS", "picture": "PICTURE", "genres": "GENRE CHANNELS", "shows": "SHOW CHANNELS"}

    def __init__(self, tv):
        self.tv = tv
        self.page = "main"
        self.sel = 0
        self.list_page = 0
        self.editing = False  # encoder is adjusting the volume

    def rows(self):
        tv, s = self.tv, self.tv.settings
        if self.page == "main":
            vol = f"◀ {s['volume']}% ▶" if self.editing else f"−  {s['volume']}%  +"
            enabled = len([g for g in tv.all_genres if g not in s["disabled_genres"]])
            return [
                ("Tune-in", option_label(TUNE_MODES, s["tune_mode"]), lambda side: tv.cycle("tune_mode", TUNE_MODES)),
                ("Content", option_label(CONTENT, s["content"]), lambda side: tv.cycle("content", CONTENT)),
                ("Anime", option_label(ANIME, s["anime"]), lambda side: tv.cycle("anime", ANIME)),
                ("Subtitles", option_label(SUBTITLES, s["subtitles"]), lambda side: tv.cycle("subtitles", SUBTITLES)),
                ("Picture", "▶", lambda side: self._goto("picture", 1)),
                ("Volume", vol, self._volume),
                ("Sleep timer", tv.sleep_label(), lambda side: tv.cycle_sleep()),
                ("Genre channels", f"{enabled} of {len(tv.all_genres)}  ▶", lambda side: self._goto("genres", 1)),
                ("Show channels", f"{len(s['show_channels'])} on  ▶", lambda side: self._goto("shows", 1)),
                ("Close", "", lambda side: tv.close_menu()),
            ]
        if self.page == "picture":
            return [
                ("◀ Back", "", lambda side: self._goto("main", 4)),
                ("Shape", option_label(ASPECT, s["aspect"]), lambda side: tv.cycle("aspect", ASPECT)),
                ("CRT effect", option_label(CRT_LEVELS, s["crt"]), lambda side: tv.cycle("crt", CRT_LEVELS)),
                ("Glitches", option_label(GLITCHES, s["glitches"]), lambda side: tv.cycle("glitches", GLITCHES)),
            ]
        if self.page == "genres":
            return self._paged([(g, "off" if g in s["disabled_genres"] else "ON",
                                 lambda side, g=g: tv.toggle_genre(g)) for g in tv.all_genres], back_to=7)
        return self._paged([(tidy(name), "ON" if sid in s["show_channels"] else "off",
                             lambda side, sid=sid: tv.toggle_show(sid)) for sid, name in tv.series()], back_to=8)

    def _paged(self, items, back_to):
        """Back, a page of toggle rows, and a page-turn row when they don't fit."""
        pages = max(1, math.ceil(len(items) / self.PER_PAGE))
        self.list_page %= pages
        start = self.list_page * self.PER_PAGE
        rows = [("◀ Back", "", lambda side: self._goto("main", back_to))] + items[start:start + self.PER_PAGE]
        if pages > 1:
            rows.append(("More", f"page {self.list_page + 1}/{pages}  ▶", lambda side: self._next_page()))
        return rows

    def _volume(self, side):
        if side is None:
            self.editing = not self.editing
        else:
            self.tv.adjust_volume(5 * side)

    def _goto(self, page, sel):
        self.page, self.sel, self.list_page = page, sel, 0

    def _next_page(self):
        self.list_page += 1

    def move(self, delta):
        if self.editing:
            self.tv.adjust_volume(5 * delta)
        else:
            self.sel = (self.sel + delta) % len(self.rows())
        self.render()

    def activate(self, side=None):
        rows = self.rows()
        rows[min(self.sel, len(rows) - 1)][2](side)
        if self.tv.menu is self:
            self.render()

    def row_height(self, rows):
        return min(self.ROW_H, (SCREEN_H - self.TOP - 4) // len(rows))

    def tap(self, x, y):
        rows = self.rows()
        idx = (y - self.TOP) // self.row_height(rows)
        if 0 <= idx < len(rows):
            self.sel, self.editing = idx, False
            self.activate(-1 if x < SCREEN_W // 2 else 1)

    def render(self):
        rows = self.rows()
        self.sel = min(self.sel, len(rows) - 1)
        w, h, rh = SCREEN_W, SCREEN_H, self.row_height(rows)
        fs = min(26, rh // 2 + 2)
        title = self.TITLES[self.page]
        ev = [rf"{{\an7\pos(0,0)\bord0\shad0\1c&H000000&\1a&H30&\p1}}m 0 0 l {w} 0 {w} {h} 0 {h}{{\p0}}",
              rf"{{\an8\pos({w // 2},14){FONT}\fs30\b1\bord0\shad0\1c{GREEN}}}{title}"]
        for i, (label, value, _) in enumerate(rows):
            y = self.TOP + i * rh
            cy = y + rh // 2
            if i == self.sel:
                ev.append(rf"{{\an7\pos(0,0)\bord0\shad0\1c{GREEN}\1a&HB8&\p1}}"
                          rf"m 24 {y + 4} l {w - 24} {y + 4} {w - 24} {y + rh - 4} 24 {y + rh - 4}{{\p0}}")
            ev.append(rf"{{\an4\pos(48,{cy}){FONT}\fs{fs}\b0\bord0\shad0\1c&HFFFFFF&}}{ass_escape(label, 26)}")
            if value:
                ev.append(rf"{{\an6\pos({w - 48},{cy}){FONT}\fs{fs}\b0\bord0\shad0\1c&HCCCCCC&}}"
                          rf"{ass_escape(value, 22)}")
        self.tv.mpv.osd(2, "\n".join(ev), z=10)


# --------------------------------------------------------------------------- the TV

class TV:
    def __init__(self, cfg):
        self.cfg = cfg
        self.settings = load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
        self.events = queue.Queue()
        self.timers = {}
        self.jf = None
        self.all_genres = []
        self.channels = []
        self.channel = 0
        self.channels_dirty = False
        self.current = None        # the programme mpv is playing (or starting)
        self.pending = None        # the programme being fetched while the test card shows
        self.card_since = None     # when the test card went up, while it's up
        self.glitches = set()      # why the glitch shader is on: "card", "random"
        self.playing_entry = None  # mpv playlist entry currently playing
        self.finished = None       # id of the programme that last played to the end
        self.failures = 0
        self.recent = deque(maxlen=30)
        self.menu = None
        self.controls_visible = False
        self.knob_down_at = None
        self.knob_used = False
        self.sleep_minutes = 0
        self.asleep = False
        self.mpv = Mpv(self._mpv_args(), lambda ev: self.post("mpv", ev))

    # ---- plumbing

    def post(self, kind, arg=None):
        self.events.put((kind, arg))

    def after(self, name, seconds, fn):
        self.timers[name] = (time.monotonic() + seconds, fn)

    def cancel(self, name):
        self.timers.pop(name, None)

    def _mpv_args(self):
        args = ["--idle=yes", "--force-window=yes", "--vo=gpu", "--gpu-context=drm", "--hwdec=no",
                "--profile=fast", "--no-input-default-bindings", "--input-terminal=no", "--osc=no",
                "--osd-level=0", "--blend-subtitles=video",  # subtitles under the CRT shader
                "--prefetch-playlist=yes", "--cache=yes", "--quiet",
                "--msg-level=all=warn,mkv=error",  # mkv: "not seekable" notice on every stream
                f"--audio-device={self.cfg['audio_device']}",
                f"--volume={self.settings['volume']}",
                f"--keepaspect={'yes' if self.settings['aspect'] == 'keep' else 'no'}"]
        if self.shaders():
            args.append(f"--glsl-shaders={','.join(self.shaders())}")
        return args

    GLITCH_SHADER = str(SHADER_DIR / "glitch.glsl")

    def shaders(self):
        """The shader chain: glitch (when on) runs on the video before the CRT look."""
        chain = [self.GLITCH_SHADER] if self.glitches else []
        if self.settings["crt"] != "off":
            chain.append(str(SHADER_DIR / f"crt-{self.settings['crt']}.glsl"))
        return chain

    def set_glitch(self, reason, on):
        before = bool(self.glitches)
        (self.glitches.add if on else self.glitches.discard)(reason)
        if bool(self.glitches) != before:
            self.mpv.set("glsl-shaders", self.shaders())

    def run(self):
        self._inputs = start_encoder(self.cfg, self.post)
        threading.Thread(target=touch_loop, args=(self.cfg, self.post), daemon=True).start()
        threading.Thread(target=fifo_loop, args=(self.post,), daemon=True).start()
        self.after("connect", 0, self.connect)
        self.schedule_glitch()
        while True:
            now = time.monotonic()
            wait = min([d for d, _ in self.timers.values()], default=now + 1) - now
            try:
                kind, arg = self.events.get(timeout=max(0.0, min(wait, 1.0)))
                self.handle(kind, arg)
            except queue.Empty:
                pass
            now = time.monotonic()
            for name, (deadline, fn) in list(self.timers.items()):
                if deadline <= now and self.timers.get(name) == (deadline, fn):
                    del self.timers[name]
                    fn()

    def handle(self, kind, arg):
        if kind not in ("mpv", "stream-ready", "stream-failed"):
            log.info("input: %s %s", kind, "" if arg is None else arg)
        if kind == "mpv":
            self.on_mpv(arg)
        elif kind == "stream-ready":
            self.on_stream_ready(arg)
        elif kind == "stream-failed":
            self.on_stream_failed(*arg)
        elif kind == "rotate":
            self.on_rotate(arg)
        elif kind == "press":
            self.on_knob_press()
        elif kind == "release":
            self.on_knob_release()
        elif kind == "click":
            self.on_click()
        elif kind == "hold":
            self.on_knob_long()
        elif kind in ("menu", "longpress"):
            self.toggle_menu()
        elif kind == "tap":
            self.on_tap(*arg)
        elif kind == "swipe":
            self.on_swipe(arg)
        elif kind == "cmd":
            self.on_command(arg)

    def on_command(self, line):
        words = line.split()
        simple = {"up": ("rotate", 1), "down": ("rotate", -1), "click": ("click", None),
                  "skip": ("click", None), "hold": ("hold", None), "menu": ("menu", None)}
        if words[0] in simple:
            self.handle(*simple[words[0]])
        elif words[0] == "tap" and len(words) == 3:
            self.on_tap(int(words[1]), int(words[2]))
        elif words[0] == "swipe" and len(words) == 2:
            self.on_swipe(words[1])
        elif words[0] == "restart":
            self.restart()
        elif words[0] == "glitch":
            self.glitch(force=True)
        elif words[0] == "quit":
            raise SystemExit(0)
        else:
            log.warning("unknown command: %s", line)

    # ---- channels

    def types(self):
        return {"both": ["Movie", "Series"], "movies": ["Movie"], "shows": ["Series"]}[self.settings["content"]]

    def connect(self):
        try:
            if self.jf is None:
                self.jf = Jellyfin(self.cfg["server"], self.cfg["api_key"], self.cfg["user"])
            self.build_channels()
        except (requests.RequestException, LookupError) as e:
            log.error("Jellyfin unavailable: %s", e)
            self.no_signal()
            self.after("connect", 10, self.connect)
            return
        self.change_channel(0)

    def build_channels(self):
        """Genre channels first, then a channel per show picked in the menu.

        Each channel is {"key", "kind": "genre"|"show", "id": genre name|series id, "name"}.
        """
        types = self.types()
        minimum = self.cfg["min_channel_items"]
        self.all_genres = [g for g in self.jf.genres(types) if len(self.pool(g)) >= minimum]
        genres = [g for g in self.all_genres if g not in self.settings["disabled_genres"]] or self.all_genres
        names = {sid: tidy(name) for sid, name in self.jf.all_series()}
        self.channels = [{"key": f"genre:{g}", "kind": "genre", "id": g, "name": g} for g in genres]
        self.channels += [{"key": f"show:{sid}", "kind": "show", "id": sid, "name": names[sid]}
                          for sid in self.settings["show_channels"] if sid in names]
        keys = [c["key"] for c in self.channels]
        last = self.settings["last_channel"] or f"genre:{self.settings.get('last_genre')}"
        self.channel = keys.index(last) if last in keys else 0
        log.info("%d channels: %s", len(self.channels), ", ".join(c["name"] for c in self.channels))

    def series(self):
        """All series for the show-channel picker (empty if Jellyfin can't be reached)."""
        try:
            return self.jf.all_series() if self.jf else []
        except requests.RequestException as e:
            log.error("Jellyfin request failed: %s", e)
            return []

    def change_channel(self, delta):
        if not self.channels:
            return
        self.channel = (self.channel + delta) % len(self.channels)
        self.show_card()
        self.show_banner([])
        # Wait for the knob to settle so fast surfing doesn't start a transcode per channel.
        self.after("tune", 0.4, lambda: self.tune("tune"))

    # ---- choosing and playing programmes

    def pool(self, genre):
        """The channel's movies/series after the content and anime settings."""
        items = self.jf.genre_pool(genre, self.types())
        anime = self.settings["anime"]
        if anime == "on":
            return items
        ids = self.jf.anime_ids()
        return [i for i in items if (i["Id"] in ids) == (anime == "only")]

    def pick(self, genre, reason):
        mode = self.settings["tune_mode"]
        if mode == "live" and reason != "skip":
            picked = self.live_pick(genre, self.finished if reason == "next" else None)
            if picked:
                return picked
        pool = self.pool(genre)
        candidates = [i for i in pool if i["Id"] not in self.recent] or list(pool)
        random.shuffle(candidates)
        for item in candidates[:5]:
            if item["Type"] != "Series":
                break
            episodes = self.jf.episodes(item["Id"])
            if episodes:
                item = random.choice(episodes)
                break
        else:
            return None, 0
        start = 0.0
        # Tuning in lands mid-programme; a programme following another starts at the top.
        if reason in ("tune", "skip") and mode != "beginning":
            start = random.uniform(0, min(self.cfg["random_start_max"], runtime_seconds(item) * 0.5))
        return item, start

    def live_pick(self, genre, finished=None):
        """Deterministic all-day schedule per channel, so it plays 'what's on now'.

        Series slots last the series' typical episode length, so only the show on air now
        needs its episodes fetched. `finished` is the programme that just ended; if the
        schedule still says it's on (listed runtime longer than the file), move on.
        """
        def slot_length(i):  # series without metadata get a typical half-hour slot
            return runtime_seconds(i) or (1440 if i["Type"] == "Series" else 0)

        pool = [i for i in self.pool(genre) if slot_length(i) > 0]
        if not pool:
            return None
        now = datetime.datetime.now()
        t = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        now = now.timestamp()
        s = self.settings
        rng = random.Random(f"{genre}|{datetime.date.today()}|{s['content']}|{s['anime']}")
        for _ in range(5000):
            item = rng.choice(pool)
            duration = slot_length(item)
            if t + duration - 30 > now and item["Id"] != finished:
                if item["Type"] == "Series":
                    episodes = self.jf.episodes(item["Id"])
                    if not episodes:
                        t += duration
                        continue
                    item = rng.choice(episodes)
                    if item["Id"] == finished:
                        t += duration
                        continue
                    return item, max(0.0, min(now - t, runtime_seconds(item) - 60))
                return item, max(0.0, now - t)
            t += duration
        return None

    def show_pick(self, series_id, reason):
        """The episode a show channel is on: resume where it was left, or move to the next
        one when the last one ended or was skipped (wrapping round after the finale)."""
        episodes = self.jf.ordered_episodes(series_id)
        if not episodes:
            return None, 0
        progress = self.settings["show_progress"].get(series_id, {})
        ids = [e["Id"] for e in episodes]
        idx = ids.index(progress["episode"]) if progress.get("episode") in ids else 0
        start = max(0.0, progress.get("position", 0) - 5)  # a few seconds of overlap
        if progress and reason in ("next", "skip"):
            idx, start = (idx + 1) % len(episodes), 0.0
        episode = dict(episodes[idx], _episode_no=idx + 1, _episode_total=len(episodes))
        return episode, start

    def tune(self, reason):
        if self.asleep or not self.channels:
            return
        ch = self.channels[self.channel]
        if self.settings["last_channel"] != ch["key"]:
            self.settings["last_channel"] = ch["key"]
            save_json(SETTINGS_PATH, self.settings)
        try:
            if ch["kind"] == "show":
                item, start = self.show_pick(ch["id"], reason)
            else:
                item, start = self.pick(ch["id"], reason)
        except requests.RequestException as e:
            log.error("Jellyfin request failed: %s", e)
            self.no_signal()
            self.after("tune", 10, lambda: self.tune(reason))
            return
        if item is None:
            self.show_banner(["NO PROGRAMMES"], seconds=8)
            return
        log.info("CH %02d %s", self.channel + 1, ch["name"])
        self.play(item, start, show=ch["id"] if ch["kind"] == "show" else None)

    def play(self, item, start, show=None):
        """Start fetching a programme; it goes on screen once data is flowing (on_stream_ready).
        Until then whatever is showing stays: the test card, or for a restart the programme
        itself. `show` is the series id on a show channel, so progress is kept."""
        self.cancel_pending()
        try:
            audio, subtitle = choose_streams(self.jf.media_streams(item["Id"]), self.settings["subtitles"])
        except requests.RequestException as e:
            log.warning("couldn't read audio/subtitle tracks: %s", e)
            audio = subtitle = None
        session = uuid.uuid4().hex
        url = self.jf.stream_url(item, self.cfg, session, start, audio, subtitle)
        relay = StreamRelay(url, lambda r: self.post("stream-ready", r),
                            lambda r, e: self.post("stream-failed", (r, e)))
        self.pending = {"relay": relay, "item": item, "session": session, "start": start, "show": show,
                        "subtitles": subtitle is not None}
        log.info("loading %s from %d:%02d (audio %s, subtitles %s)", " / ".join(describe(item)),
                 start // 60, start % 60, audio, subtitle)

    def on_stream_ready(self, relay):
        pending = self.pending
        if not pending or pending["relay"] is not relay:
            return  # superseded meanwhile
        # Keep the card up a moment even when the server is quick, so a channel change registers.
        shown_for = time.monotonic() - self.card_since if self.card_since else math.inf
        wait = self.cfg["card_min_seconds"] - shown_for
        if wait > 0:
            self.after("on-air", wait, lambda: self.on_stream_ready(relay))
            return
        self.pending = None
        self.end_current()
        # The stream carries at most the one subtitle track we asked Jellyfin for.
        reply = self.mpv.command("loadfile", relay.path, "replace", -1,
                                 "sid=1" if pending["subtitles"] else "sid=no")
        if not reply:
            relay.cancel()
            return self.programme_failed("mpv refused the stream")
        item = pending["item"]
        self.recent.append(item.get("SeriesId") or item["Id"])
        self.current = dict(pending, entry=reply.get("playlist_entry_id"), shown=False)
        if pending["show"]:
            self.save_progress(pending["show"], item["Id"], pending["start"])
            self.after("progress", 30, self.track_progress)

    def on_stream_failed(self, relay, error):
        if self.pending and self.pending["relay"] is relay:
            self.jf.stop_transcode(self.pending["session"])
            self.pending = None
            self.programme_failed(f"stream failed: {error}")

    def programme_failed(self, why):
        log.error("%s", why)
        self.failures += 1
        if self.failures < 3:
            self.show_card()
            self.after("tune", 1, lambda: self.tune("skip"))
        else:
            self.failures = 0
            self.no_signal()
            self.after("tune", 30, lambda: self.tune("tune"))

    def cancel_pending(self):
        self.cancel("on-air")
        if self.pending:
            self.pending["relay"].cancel()
            self.jf.stop_transcode(self.pending["session"])
            self.pending = None

    def end_current(self):
        if self.current:
            self.record_position()
            self.current["relay"].cancel()
            self.jf.stop_transcode(self.current["session"])
            self.current = None

    # ---- show channel progress (kept in settings.json, never sent to Jellyfin)

    def save_progress(self, series_id, episode_id, position):
        self.settings["show_progress"][series_id] = {"episode": episode_id, "position": round(position)}
        save_json(SETTINGS_PATH, self.settings)

    def position(self):
        """Seconds into the current programme, if it's what's on screen."""
        cur = self.current
        if not (cur and cur["shown"] and self.playing_entry == cur["entry"]):
            return None
        elapsed = self.mpv.get("time-pos")  # counts from where the stream started
        return None if elapsed is None else cur["start"] + elapsed

    def record_position(self):
        pos = self.position()
        if self.current and self.current["show"] and pos is not None:
            self.save_progress(self.current["show"], self.current["item"]["Id"], pos)

    def track_progress(self):
        if self.current and self.current["show"]:
            self.record_position()
            self.after("progress", 30, self.track_progress)

    def reload_current(self):
        """Restart the stream at the same point, e.g. to apply a new subtitle setting."""
        pos = self.position()
        if pos is not None:
            self.play(self.current["item"], pos, show=self.current["show"])

    def show_card(self):
        """Put up the test card (glitching, if glitches are on) until the next programme is ready."""
        self.cancel_pending()
        if self.card_since is not None and self.current is None:
            return  # already up (e.g. surfing past several channels)
        self.end_current()
        self.mpv.command("loadfile", testcard_url(), "replace")
        self.card_since = time.monotonic()
        self.set_glitch("card", self.settings["glitches"] == "on")

    def no_signal(self):
        self.show_card()
        self.show_banner(["NO SIGNAL"], seconds=3600)

    def on_mpv(self, ev):
        name = ev.get("event")
        cur = self.current
        if name == "start-file":
            self.playing_entry = ev.get("playlist_entry_id")
        elif name == "playback-restart" and cur and self.playing_entry == cur["entry"] and not cur["shown"]:
            cur["shown"] = True
            self.card_since = None
            self.set_glitch("card", False)
            self.failures = 0
            self.show_banner()
        elif name == "end-file" and cur and ev.get("playlist_entry_id") == cur["entry"]:
            if ev.get("reason") == "eof":
                self.finished = cur["item"]["Id"]
                self.show_card()
                self.after("tune", 0.1, lambda: self.tune("next"))
            elif ev.get("reason") == "error":
                self.end_current()
                self.programme_failed(f"playback failed: {ev.get('file_error')}")
        elif name == "mpv-exit":
            raise SystemExit("mpv exited")

    # ---- on-screen banner

    def show_banner(self, lines=None, seconds=5.0):
        if self.menu:
            return
        head = f"CH {self.channel + 1:02d}  {self.channels[self.channel]['name'].upper()}" if self.channels else "CRT TV"
        if lines is None:
            lines = describe(self.current["item"]) if self.current else []
        ev = [rf"{{\an7\pos(48,40){FONT}\fs34\b1\bord3\shad0\1c{GREEN}\3c&H000000&}}{ass_escape(head, 36)}"]
        y = 86
        for i, text in enumerate(lines):
            size = 26 if i == 0 else 22
            ev.append(rf"{{\an7\pos(48,{y}){FONT}\fs{size}\b0\bord2.5\shad0\1c&HFFFFFF&\3c&H000000&}}"
                      rf"{ass_escape(text)}")
            y += size + 8
        self.mpv.osd(1, "\n".join(ev))
        self.after("banner", seconds, lambda: self.mpv.osd_clear(1))

    # ---- input

    def on_rotate(self, delta):
        if self.asleep:
            self.wake()
        elif self.menu:
            self.menu.move(delta)
            self.after("menu", 30, self.close_menu)
        else:
            self.change_channel(delta)

    KNOB_HOLD, KNOB_MENU = 1.0, 3.0  # seconds: release after HOLD = restart, reach MENU = menu

    def on_knob_press(self):
        self.knob_down_at = time.monotonic()
        self.knob_used = False
        self.after("knob-hint", self.KNOB_HOLD, self.knob_hint)
        self.after("knob-menu", self.KNOB_MENU, self.knob_menu)

    def knob_hint(self):
        if self.current and not self.menu and not self.asleep:
            self.show_banner(["◀◀ RELEASE: FROM START", "KEEP HOLDING: MENU"], seconds=2.5)

    def knob_menu(self):
        self.knob_used = True
        self.toggle_menu()

    def on_knob_release(self):
        if self.knob_down_at is None:
            return
        held = time.monotonic() - self.knob_down_at
        self.knob_down_at = None
        self.cancel("knob-hint")
        self.cancel("knob-menu")
        if self.knob_used:
            return
        if held < self.KNOB_HOLD:
            self.on_click()
        else:
            self.on_knob_long()

    def on_knob_long(self):
        if self.asleep:
            self.wake()
        elif self.menu:
            self.close_menu()
        else:
            self.restart()

    def on_click(self):
        if self.asleep:
            self.wake()
        elif self.menu:
            self.menu.activate()
            self.after("menu", 30, self.close_menu)
        else:
            self.skip()

    # ---- random sync glitches

    def schedule_glitch(self):
        self.after("glitch", random.uniform(*self.cfg["glitch_every"]), self.glitch)

    def glitch(self, force=False):
        """Briefly lose sync: switch the glitch shader on for a fraction of a second."""
        if not force:
            self.schedule_glitch()
            on_air = self.current and self.current["shown"]
            if self.settings["glitches"] != "on" or not on_air or self.menu or self.asleep:
                return
        self.set_glitch("random", True)
        self.after("glitch-end", random.uniform(*self.cfg["glitch_length"]),
                   lambda: self.set_glitch("random", False))

    def restart(self):
        """Play the current programme again from the top (it keeps playing until then)."""
        if self.current:
            self.play(self.current["item"], 0, show=self.current["show"])

    def skip(self):
        if self.channels:
            self.show_card()
            self.show_banner([])
            self.after("tune", 0.1, lambda: self.tune("skip"))

    def on_tap(self, x, y):
        if self.asleep:
            self.wake()
        elif self.menu:
            self.menu.tap(x, y)
            self.after("menu", 30, self.close_menu)
        elif self.controls_visible:
            button = self.control_at(x, y)
            if button is None:
                self.hide_controls()
            else:
                self.press_control(button)
        else:
            self.show_banner()
            self.show_controls()

    def on_swipe(self, direction):
        if self.asleep:
            self.wake()
        elif not self.menu:
            {"up": lambda: self.change_channel(1), "down": lambda: self.change_channel(-1),
             "left": self.skip, "right": self.skip}[direction]()

    # ---- on-screen control bar (touch)

    CONTROLS = ["CH ▼", "CH ▲", "◀◀ START", "SKIP ▶▶", "VOL −", "VOL +", "MENU"]
    BAR_X, BAR_Y, BUTTON_W, BUTTON_H, BUTTON_GAP = 16, 398, 102, 66, 9

    def control_at(self, x, y):
        offset = x - self.BAR_X
        idx = offset // (self.BUTTON_W + self.BUTTON_GAP)
        if self.BAR_Y - 10 <= y and 0 <= offset and idx < len(self.CONTROLS):
            return idx
        return None

    def press_control(self, idx):
        [lambda: self.change_channel(-1), lambda: self.change_channel(1), self.restart, self.skip,
         lambda: self.adjust_volume(-5), lambda: self.adjust_volume(5), self.toggle_menu][idx]()
        if self.controls_visible:
            self.show_controls(pressed=idx)
            self.after("controls-flash", 0.2, lambda: self.controls_visible and self.render_controls())

    def show_controls(self, pressed=None):
        self.controls_visible = True
        self.render_controls(pressed)
        self.after("controls", 5, self.hide_controls)

    def render_controls(self, pressed=None):
        y, w, h = self.BAR_Y, self.BUTTON_W, self.BUTTON_H
        ev = [rf"{{\an9\pos({SCREEN_W - 48},40){FONT}\fs26\b1\bord3\shad0\1c{GREEN}\3c&H000000&}}"
              rf"VOL {self.settings['volume']}%"]
        for i, label in enumerate(self.CONTROLS):
            x = self.BAR_X + i * (w + self.BUTTON_GAP)
            fill = rf"\1c{GREEN}\1a&H40&" if i == pressed else r"\1c&H000000&\1a&H50&"
            ev.append(rf"{{\an7\pos(0,0)\bord2\shad0{fill}\3c{GREEN}\3a&H40&\p1}}"
                      rf"m {x} {y} l {x + w} {y} {x + w} {y + h} {x} {y + h}{{\p0}}")
            ev.append(rf"{{\an5\pos({x + w // 2},{y + h // 2}){FONT}\fs20\b1\bord0\shad0\1c&HFFFFFF&}}{label}")
        self.mpv.osd(3, "\n".join(ev), z=5)

    def hide_controls(self):
        self.controls_visible = False
        self.cancel("controls")
        self.mpv.osd_clear(3)

    def toggle_menu(self):
        if self.asleep:
            return self.wake()
        if self.menu:
            return self.close_menu()
        self.hide_controls()
        self.cancel("banner")
        self.mpv.osd_clear(1)
        self.menu = Menu(self)
        self.menu.render()
        self.after("menu", 30, self.close_menu)

    def close_menu(self):
        if not self.menu:
            return
        self.menu = None
        self.cancel("menu")
        self.mpv.osd_clear(2)
        if self.channels_dirty:
            self.channels_dirty = False
            self.rebuild_channels()

    # ---- settings

    def cycle(self, key, options):
        self.settings[key] = next_option(options, self.settings[key])
        save_json(SETTINGS_PATH, self.settings)
        if key == "crt":
            self.mpv.set("glsl-shaders", self.shaders())
        elif key == "glitches" and self.card_since is not None:
            self.set_glitch("card", self.settings["glitches"] == "on")
        elif key == "aspect":
            self.mpv.set("keepaspect", self.settings["aspect"] == "keep")
        elif key in ("content", "anime"):
            # What's on may no longer belong, so retune even if the channel survives.
            self.rebuild_channels(retune=True)
        elif key == "subtitles":
            self.reload_current()

    def rebuild_channels(self, retune=False):
        before = self.channels[self.channel]["key"] if self.channels else None
        try:
            self.build_channels()
        except requests.RequestException as e:
            log.error("Jellyfin request failed: %s", e)
            return
        if retune or not self.channels or self.channels[self.channel]["key"] != before:
            self.change_channel(0)

    def toggle_show(self, series_id):
        shows = self.settings["show_channels"]
        if series_id in shows:
            shows.remove(series_id)
        else:
            shows.append(series_id)
        save_json(SETTINGS_PATH, self.settings)
        self.channels_dirty = True

    def toggle_genre(self, genre):
        disabled = self.settings["disabled_genres"]
        if genre in disabled:
            disabled.remove(genre)
        elif len([g for g in self.all_genres if g not in disabled]) > 1:
            disabled.append(genre)
        save_json(SETTINGS_PATH, self.settings)
        self.channels_dirty = True

    def adjust_volume(self, delta):
        self.settings["volume"] = max(0, min(100, self.settings["volume"] + delta))
        self.mpv.set("volume", self.settings["volume"])
        save_json(SETTINGS_PATH, self.settings)

    def sleep_label(self):
        if not self.sleep_minutes:
            return "Off"
        left = math.ceil((self.timers["sleep"][0] - time.monotonic()) / 60) if "sleep" in self.timers else 0
        return f"{self.sleep_minutes}m ({left}m left)"

    def cycle_sleep(self):
        self.sleep_minutes = SLEEP_MINUTES[(SLEEP_MINUTES.index(self.sleep_minutes) + 1) % len(SLEEP_MINUTES)]
        if self.sleep_minutes:
            self.after("sleep", self.sleep_minutes * 60, self.sleep)
        else:
            self.cancel("sleep")

    def sleep(self):
        log.info("sleep timer: going to sleep")
        self.close_menu()
        self.hide_controls()
        self.sleep_minutes = 0
        self.asleep = True
        self.cancel("tune")
        self.cancel_pending()
        self.end_current()
        self.mpv.command("stop")
        self.card_since = None
        self.set_glitch("card", False)
        self.mpv.osd_clear(1)
        set_backlight(False)

    def wake(self):
        self.asleep = False
        set_backlight(True)
        self.change_channel(0)


def set_backlight(on):
    for bl in Path("/sys/class/backlight").glob("*"):
        try:
            (bl / "bl_power").write_text("0" if on else "1")
        except OSError:
            pass  # needs a udev rule to be writable; the screen just stays black instead


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not cfg["api_key"]:
        sys.exit(f"Set api_key in {CONFIG_PATH} (Jellyfin Dashboard → API Keys)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    tv = TV(cfg)
    try:
        tv.run()
    finally:
        tv.cancel_pending()
        tv.end_current()
        tv.mpv.close()


if __name__ == "__main__":
    main()
