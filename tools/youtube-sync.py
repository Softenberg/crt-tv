#!/usr/bin/env python3
"""Keep a local copy of a YouTube playlist for the TV's YouTube channel.

Works newest video first: downloads until the space budget is used, deleting the oldest
local videos to make room for newer ones. Files are named "<title> [<id>].<ext>", so a video
that's already on disk is never downloaded again. Writes youtube/index.json for tv.py.

tv.py runs this every few hours; you can also run it by hand: python3 tools/youtube-sync.py
Settings (config.json): youtube_playlist, youtube_max_gb, youtube_min_free_gb,
youtube_rate_limit, youtube_newest ("last" if new videos are added at the end of the playlist).
"""
import fcntl
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import tv  # noqa: E402  (config defaults and helpers)

VIDEO_DIR = BASE / "youtube"
INDEX = VIDEO_DIR / "index.json"
VIDEO_EXTS = {".mkv", ".mp4", ".webm"}
# 480p at most (the screen is 800x480), preferring H.264, which the Pi decodes most easily.
FORMAT = "bv*[height<=480][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b"
GB = 1_000_000_000


def log(msg):
    print(f"youtube-sync: {msg}", flush=True)


def update_ytdlp():
    """Keep the project's yt-dlp current (YouTube breaks old versions); at most weekly."""
    pip = BASE / ".venv" / "bin" / "pip"
    stamp = BASE / ".venv" / ".yt-dlp-updated"
    if not pip.exists() or (stamp.exists() and time.time() - stamp.stat().st_mtime < 7 * 86400):
        return
    out = subprocess.run([str(pip), "install", "-q", "-U", "yt-dlp[default]"], capture_output=True, text=True)
    if out.returncode == 0:
        stamp.touch()
    else:
        log(f"couldn't update yt-dlp: {out.stderr.strip()[-200:]}")


def ytdlp():
    venv = BASE / ".venv" / "bin" / "yt-dlp"  # a current yt-dlp (YouTube breaks old ones)
    cmd = [str(venv) if venv.exists() else "yt-dlp"]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]  # YouTube needs a JavaScript runtime nowadays
    return cmd


def playlist(url):
    """Playlist entries as dicts with id, title and duration, in playlist order."""
    out = subprocess.run(ytdlp() + ["--flat-playlist", "-J", url], capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[-500:])
    return [e for e in json.loads(out.stdout).get("entries", []) if e and e.get("id")]


def local_videos():
    """{video id: path} of what's already downloaded; tidies away unfinished downloads."""
    found = {}
    for path in VIDEO_DIR.iterdir():
        leftover = re.search(r"\]\.f\d+\.\w+$", path.name)  # a stream that was never merged
        if path.suffix in (".part", ".ytdl") or ".part-Frag" in path.name or ".temp." in path.name or leftover:
            path.unlink(missing_ok=True)
            continue
        m = re.search(r"\[([\w-]{11})\]\.\w+$", path.name)
        if m and path.suffix in VIDEO_EXTS:
            found[m.group(1)] = path
    return found


def download(video_id, rate_limit):
    out = subprocess.run(ytdlp() + [
        "-f", FORMAT, "--merge-output-format", "mkv", "--no-playlist", "--no-progress",
        "--limit-rate", rate_limit, "-o", str(VIDEO_DIR / "%(title).80B [%(id)s].%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}"], capture_output=True, text=True)
    if out.returncode:
        log(f"couldn't download {video_id}: {out.stderr.strip().splitlines()[-1:]}")
        return None
    return local_videos().get(video_id)


def main():
    cfg = tv.load_json(tv.CONFIG_PATH, tv.DEFAULT_CONFIG)
    if not cfg["youtube_playlist"]:
        log("no youtube_playlist in config.json, nothing to do")
        return
    VIDEO_DIR.mkdir(exist_ok=True)
    lock = open(VIDEO_DIR / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("already running")
        return

    update_ytdlp()
    entries = playlist(cfg["youtube_playlist"])
    if cfg["youtube_newest"] == "last":
        entries.reverse()  # newest first from here on
    rank = {e["id"]: i for i, e in enumerate(entries)}
    have = local_videos()
    budget = cfg["youtube_max_gb"] * GB
    min_free = cfg["youtube_min_free_gb"] * GB
    log(f"{len(entries)} videos in the playlist, {len(have)} on disk")

    def used():
        return sum(p.stat().st_size for p in have.values())

    def short_of_space(extra=0):
        return used() + extra > budget or shutil.disk_usage(VIDEO_DIR).free - extra < min_free

    # Videos bigger than the whole budget, so they aren't fetched (and thrown away) every sync.
    too_big_file = VIDEO_DIR / ".too-big.json"
    try:
        too_big = json.loads(too_big_file.read_text())
    except (OSError, ValueError):
        too_big = {}
    too_big = {vid: b for vid, b in too_big.items() if b == budget}  # retry if the budget changed

    def make_room(keep_rank):
        """Delete the oldest video ranked after keep_rank (anything no longer in the playlist
        goes first). False if there's nothing older left to delete."""
        older = [vid for vid in have if rank.get(vid, len(rank)) > keep_rank]
        if not older:
            return False
        oldest = max(older, key=lambda vid: rank.get(vid, len(rank)))
        log(f"making room: deleting {have[oldest].name}")
        have.pop(oldest).unlink(missing_ok=True)
        return True

    for i, entry in enumerate(entries):
        if entry["id"] in have or entry["id"] in too_big:
            continue
        # Make room by dropping older videos, judging the size by the ones we have, and stop
        # before downloading something that would only be deleted again.
        estimate = used() / len(have) if have else 0
        while short_of_space(estimate) and make_room(i):
            pass
        if short_of_space(estimate):
            log("no room for older videos; stopping here")
            break
        log(f"downloading {entry.get('title') or entry['id']}")
        path = download(entry["id"], cfg["youtube_rate_limit"])
        if path is None:
            continue
        have[entry["id"]] = path
        while short_of_space() and make_room(i):
            pass
        if short_of_space():
            log(f"no room for {path.name}; removing it")
            have.pop(entry["id"]).unlink(missing_ok=True)
            if not have:
                too_big[entry["id"]] = budget
                continue  # bigger than the whole budget on its own; an older one may fit
            break

    too_big_file.write_text(json.dumps(too_big))

    meta = {e["id"]: e for e in entries}
    index = {vid: {"file": path.name, "title": meta.get(vid, {}).get("title") or path.stem,
                   "duration": meta.get(vid, {}).get("duration") or 0, "rank": rank.get(vid)}
             for vid, path in have.items()}
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(INDEX)
    log(f"done: {len(have)} videos, {used() / GB:.1f} GB")


if __name__ == "__main__":
    main()
