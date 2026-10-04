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
import json
import logging
import math
import os
import queue
import random
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
    "static_seconds": 1.2,
    "glitch_every": [300, 1200],  # seconds between random sync glitches (min, max)
    "glitch_length": [0.25, 1.0],
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
    "volume": 80,
    "disabled_genres": [],
    "last_genre": None,
}

TUNE_MODES = [("random", "Random (first 20m)"), ("beginning", "From beginning"), ("live", "Live schedule")]
CONTENT = [("both", "Movies + shows"), ("movies", "Movies only"), ("shows", "Shows only")]
ANIME = [("on", "Included"), ("off", "Excluded"), ("only", "Anime only")]
ASPECT = [("stretch", "Stretch"), ("keep", "Keep ratio")]
CRT_LEVELS = [("off", "Off"), ("light", "Light"), ("medium", "Medium"), ("heavy", "Heavy")]
GLITCHES = [("on", "Sometimes"), ("off", "Off")]
SLEEP_MINUTES = [0, 30, 60, 90]


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


def runtime_seconds(item):
    return (item.get("RunTimeTicks") or 0) / TICKS_PER_SEC


def describe(item):
    """Title lines for the on-screen banner."""
    if item.get("Type") == "Episode":
        line = item.get("SeriesName") or ""
        if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
            line += f" · S{item['ParentIndexNumber']:02d}E{item['IndexNumber']:02d}"
        return [line, item.get("Name") or ""]
    year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    return [f"{item.get('Name') or ''}{year}"]


def static_url(seconds):
    return (f"av://lavfi:color=c=gray:s=400x240:r=30:d={seconds},format=yuv420p,"
            f"noise=c0s=100:c0f=t+u[out0];anoisesrc=d={seconds}:c=white:a=0.06[out1]")


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

    def stream_url(self, item, cfg, session, start=0.0):
        # A progressive transcode that starts at the tune-in point: one ffmpeg job per
        # programme and no seeking (mpv's HLS demuxer always fetches segment 0 first,
        # making Jellyfin start and kill a second transcode).
        params = {
            "ApiKey": self.api_key,
            "MediaSourceId": item["Id"],
            "DeviceId": self.DEVICE_ID,
            "PlaySessionId": session,
            "Container": "ts",
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
        return f"{self.server}/Videos/{item['Id']}/stream.ts?{urlencode(params)}"

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

    def _send(self, command, timeout=5):
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
            log.warning("mpv: %s failed: %s", name, slot[1].get("error"))
            return None
        return slot[1].get("data")

    def command(self, *args):
        return self._send(list(args))

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

    def __init__(self, tv):
        self.tv = tv
        self.page = "main"
        self.sel = 0
        self.chan_page = 0
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
                ("Picture", option_label(ASPECT, s["aspect"]), lambda side: tv.cycle("aspect", ASPECT)),
                ("CRT effect", option_label(CRT_LEVELS, s["crt"]), lambda side: tv.cycle("crt", CRT_LEVELS)),
                ("Glitches", option_label(GLITCHES, s["glitches"]), lambda side: tv.cycle("glitches", GLITCHES)),
                ("Volume", vol, self._volume),
                ("Sleep timer", tv.sleep_label(), lambda side: tv.cycle_sleep()),
                ("Channels", f"{enabled} of {len(tv.all_genres)}  ▶", lambda side: self._goto("channels", 1)),
                ("Close", "", lambda side: tv.close_menu()),
            ]
        genres = tv.all_genres
        pages = max(1, math.ceil(len(genres) / self.PER_PAGE))
        self.chan_page %= pages
        start = self.chan_page * self.PER_PAGE
        rows = [("◀ Back", "", lambda side: self._goto("main", 8))]
        for g in genres[start:start + self.PER_PAGE]:
            rows.append((g, "off" if g in s["disabled_genres"] else "ON", lambda side, g=g: tv.toggle_genre(g)))
        if pages > 1:
            rows.append(("More", f"page {self.chan_page + 1}/{pages}  ▶", lambda side: self._next_page()))
        return rows

    def _volume(self, side):
        if side is None:
            self.editing = not self.editing
        else:
            self.tv.adjust_volume(5 * side)

    def _goto(self, page, sel):
        self.page, self.sel, self.chan_page = page, sel, 0

    def _next_page(self):
        self.chan_page += 1

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
        title = "SETTINGS" if self.page == "main" else "CHANNELS"
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
        self.current = None        # the programme we asked mpv to play
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
                "--osd-level=0", "--sid=no", "--prefetch-playlist=yes", "--cache=yes", "--quiet",
                "--msg-level=all=warn", f"--audio-device={self.cfg['audio_device']}",
                f"--volume={self.settings['volume']}",
                f"--keepaspect={'yes' if self.settings['aspect'] == 'keep' else 'no'}"]
        if self.settings["crt"] != "off":
            args.append(f"--glsl-shaders={SHADER_DIR / ('crt-' + self.settings['crt'] + '.glsl')}")
        return args

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
        if kind != "mpv":
            log.info("input: %s %s", kind, "" if arg is None else arg)
        if kind == "mpv":
            self.on_mpv(arg)
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
        types = self.types()
        minimum = self.cfg["min_channel_items"]
        self.all_genres = [g for g in self.jf.genres(types) if len(self.pool(g)) >= minimum]
        enabled = [g for g in self.all_genres if g not in self.settings["disabled_genres"]]
        self.channels = enabled or self.all_genres
        last = self.settings["last_genre"]
        self.channel = self.channels.index(last) if last in self.channels else 0
        log.info("%d channels: %s", len(self.channels), ", ".join(self.channels))

    def change_channel(self, delta):
        if not self.channels:
            return
        self.channel = (self.channel + delta) % len(self.channels)
        self.show_static()
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

    def tune(self, reason):
        if self.asleep or not self.channels:
            return
        genre = self.channels[self.channel]
        if self.settings["last_genre"] != genre:
            self.settings["last_genre"] = genre
            save_json(SETTINGS_PATH, self.settings)
        try:
            item, start = self.pick(genre, reason)
        except requests.RequestException as e:
            log.error("Jellyfin request failed: %s", e)
            self.no_signal()
            self.after("tune", 10, lambda: self.tune(reason))
            return
        if item is None:
            self.show_banner(["NO PROGRAMMES"], seconds=8)
            return
        self.play(item, start)

    def play(self, item, start, replace=False):
        """Queue a programme after the static, or with replace=True cut straight to it."""
        self.end_current()
        session = uuid.uuid4().hex
        url = self.jf.stream_url(item, self.cfg, session, start)
        reply = self.mpv.command("loadfile", url, "replace" if replace else "append-play")
        if not reply:
            return
        self.recent.append(item.get("SeriesId") or item["Id"])
        self.current = {"entry": reply.get("playlist_entry_id"), "item": item, "session": session, "shown": False}
        log.info("playing %s from %d:%02d", " / ".join(describe(item)), start // 60, start % 60)

    def end_current(self):
        if self.current:
            self.jf.stop_transcode(self.current["session"])
            self.current = None

    def show_static(self, loop=False):
        self.end_current()
        self.mpv.command("loadfile", static_url(self.cfg["static_seconds"]), "replace", -1,
                         "loop-file=inf" if loop else "")

    def no_signal(self):
        self.show_static(loop=True)
        self.show_banner(["NO SIGNAL"], seconds=3600)

    def on_mpv(self, ev):
        name = ev.get("event")
        cur = self.current
        if name == "start-file":
            self.playing_entry = ev.get("playlist_entry_id")
        elif name == "playback-restart" and cur and self.playing_entry == cur["entry"] and not cur["shown"]:
            cur["shown"] = True
            self.failures = 0
            self.show_banner()
        elif name == "end-file" and cur and ev.get("playlist_entry_id") == cur["entry"]:
            if ev.get("reason") == "eof":
                self.finished = cur["item"]["Id"]
                self.end_current()
                self.after("tune", 0.5, lambda: self.tune("next"))
            elif ev.get("reason") == "error":
                log.error("playback failed: %s", ev.get("file_error"))
                self.end_current()
                self.failures += 1
                if self.failures < 3:
                    self.after("tune", 1, lambda: self.tune("skip"))
                else:
                    self.failures = 0
                    self.no_signal()
                    self.after("tune", 30, lambda: self.tune("tune"))
        elif name == "mpv-exit":
            raise SystemExit("mpv exited")

    # ---- on-screen banner

    def show_banner(self, lines=None, seconds=5.0):
        if self.menu:
            return
        head = f"CH {self.channel + 1:02d}  {self.channels[self.channel].upper()}" if self.channels else "CRT TV"
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

    GLITCH_SHADER = str(SHADER_DIR / "glitch.glsl")

    def schedule_glitch(self):
        self.after("glitch", random.uniform(*self.cfg["glitch_every"]), self.glitch)

    def glitch(self, force=False):
        """Briefly lose sync: switch the glitch shader on for a fraction of a second."""
        if not force:
            self.schedule_glitch()
            on_air = self.current and self.current["shown"]
            if self.settings["glitches"] != "on" or not on_air or self.menu or self.asleep:
                return
        self.mpv.command("change-list", "glsl-shaders", "pre", self.GLITCH_SHADER)
        self.after("glitch-end", random.uniform(*self.cfg["glitch_length"]),
                   lambda: self.mpv.command("change-list", "glsl-shaders", "remove", self.GLITCH_SHADER))

    def restart(self):
        """Play the current programme again from the top."""
        if self.current:
            self.play(self.current["item"], 0, replace=True)

    def skip(self):
        if self.channels:
            self.show_static()
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
            if self.settings["crt"] == "off":
                self.mpv.command("change-list", "glsl-shaders", "clr", "")
            else:
                shader = SHADER_DIR / f"crt-{self.settings['crt']}.glsl"
                self.mpv.command("change-list", "glsl-shaders", "set", str(shader))
        elif key == "aspect":
            self.mpv.set("keepaspect", self.settings["aspect"] == "keep")
        elif key in ("content", "anime"):
            # What's on may no longer belong, so retune even if the channel survives.
            self.rebuild_channels(retune=True)

    def rebuild_channels(self, retune=False):
        before = self.channels[self.channel] if self.channels else None
        try:
            self.build_channels()
        except requests.RequestException as e:
            log.error("Jellyfin request failed: %s", e)
            return
        if retune or not self.channels or self.channels[self.channel] != before:
            self.change_channel(0)

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
        self.end_current()
        self.mpv.command("stop")
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
        tv.end_current()
        tv.mpv.close()


if __name__ == "__main__":
    main()
