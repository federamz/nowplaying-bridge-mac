"""
NowPlaying Bridge for macOS — exposes what your Mac is playing as a local JSON API.

Same contract as the Windows bridge: http://127.0.0.1:5788/now-playing, same
field names, same port. A widget cannot tell the two apart, and does not need to.

WHY APPLESCRIPT AND NOT THE SYSTEM NOW-PLAYING API

macOS has a system-wide now-playing service (MediaRemote), which is what the
menu bar and Control Center read. From macOS 15.4 Apple gated it behind a
private entitlement, so every third-party tool that used it stopped working.
The only ways around that are code injection with SIP disabled — unacceptable
for software people buy — or asking each player directly through AppleScript,
which is public, supported, and needs no security changes.

The trade-off is honest and worth stating: AppleScript talks to APPS, not to the
system. Music.app and Spotify.app answer beautifully, with an exact playback
position the Windows API cannot even provide. A browser tab answers not at all,
because browsers expose no scripting for media. Web players therefore still need
the Last.fm route.

Standard library only. No pip install, no third-party runtime.
"""

import argparse
import base64
import configparser
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME = "NowPlaying Bridge"
VERSION = "1.0.3-mac"
DEFAULT_PORT = 5788
POLL_SECONDS = 0.5   # osascript is a process launch, so slower than the Windows loop
QUIET = False

# Fields come back one per LINE.
#
# Two hard-won rules about generating AppleScript:
#
# 1. Never paste a raw control byte into the source — the parser rejects it
#    (-2741). `linefeed` is a built-in constant, so no escaping is involved.
# 2. Never use short variable names inside a `tell application` block. Terms are
#    resolved against the app's dictionary first, and a two-letter name can
#    collide with the app's own terminology, which also reports as -2741. Every
#    variable here is prefixed for that reason.
#
# A newline cannot occur inside a track name, so one field per line is
# unambiguous and needs no delimiter of our own.
SEP = "\n"


# --------------------------------------------------------------------------
# Living next to the app: settings, crash logs, one instance only.
# --------------------------------------------------------------------------
def app_dir():
    """
    The folder a user can actually see.

    Inside a .app bundle the executable lives in Contents/MacOS, which is not
    somewhere anyone will look for a settings file, so walk out to the folder
    holding the bundle itself.
    """
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
        parts = here.split(os.sep)
        if "Contents" in parts:
            bundle = os.sep.join(parts[: parts.index("Contents")])
            return os.path.dirname(bundle)
        return here
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_TEMPLATE = """; NowPlaying Bridge settings
;
; port  which port the bridge serves on. Change it only if another program
;       already uses 5788 (the app will tell you if that happens).
; host  127.0.0.1 means this Mac only, which is what you want. Set 0.0.0.0
;       only if you run OBS on a different machine on your network.

[server]
host = 127.0.0.1
port = 5788
"""


def load_settings():
    path = os.path.join(app_dir(), "settings.ini")
    parser = configparser.ConfigParser()
    parser.read_string(SETTINGS_TEMPLATE)
    if os.path.exists(path):
        try:
            parser.read(path, encoding="utf-8")
        except Exception:
            pass
    else:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SETTINGS_TEMPLATE)
        except OSError:
            pass
    host = parser.get("server", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
    try:
        port = parser.getint("server", "port", fallback=DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT
    return host, port


def log_crash(exc):
    """A windowed app has nowhere to print a traceback, so write one down."""
    try:
        folder = os.path.join(app_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        with open(os.path.join(folder, f"crash {stamp}.txt"), "w", encoding="utf-8") as handle:
            handle.write(f"{APP_NAME} {VERSION}\n")
            handle.write(f"Time: {stamp}\n")
            handle.write(f"Python: {sys.version}\n")
            handle.write(f"macOS: {mac_version()}\n\n")
            handle.write(f"{exc}\n\n")
            handle.write(traceback.format_exc())
        logs = sorted(
            (os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".txt")),
            key=os.path.getmtime,
        )
        for stale in logs[:-10]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception:
        pass


def mac_version():
    try:
        import platform
        return platform.mac_ver()[0] or "unknown"
    except Exception:
        return "unknown"


def already_running(host, port):
    """Ask the port whether it is one of ours, rather than keeping a lock file."""
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((probe, port), timeout=0.6) as sock:
            sock.sendall(
                f"GET /health HTTP/1.0\r\nHost: {probe}\r\nConnection: close\r\n\r\n".encode()
            )
            reply = b""
            while len(reply) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                reply += chunk
    except OSError:
        return False
    return APP_NAME.encode() in reply


def safe_print(*parts):
    if not getattr(sys, "stdout", None):
        return
    try:
        print(" ".join(str(p) for p in parts), flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Talking to players
# --------------------------------------------------------------------------
def run_applescript(source, timeout=4.0):
    """
    Run AppleScript, returning (stdout, error_or_None).

    Automation is permission-gated: the first call raises a system prompt, and a
    refusal comes back as error -1743 forever after. That is a support problem,
    not a bug, so it is detected and reported in words rather than swallowed.
    """
    try:
        done = subprocess.run(
            ["/usr/bin/osascript", "-e", source],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except OSError as exc:
        return "", f"cannot run osascript: {exc}"
    if done.returncode != 0:
        err = (done.stderr or "").strip()
        if "-1743" in err or "not allowed assistive" in err or "not authorised" in err:
            return "", "permission"
        if "-1728" in err:          # "can't get current track" — nothing loaded
            return "", None
        return "", err[:200] or "applescript error"
    return done.stdout.strip(), None


# Each adapter asks one app for everything in a single call. `is running` is
# checked first and never launches the app — an overlay that opened iTunes
# because someone loaded a browser source would be a genuine disaster.
MUSIC_SCRIPT = """
if application "Music" is not running then return ""
tell application "Music"
  set npState to (player state as text)
  if npState is "stopped" then return "stopped"
  try
    set npTrack to current track
  on error
    return "stopped"
  end try
  set npName to ""
  try
    set npName to (name of npTrack) as text
  end try
  if npName is "" then return "stopped"
  set npArtist to ""
  try
    set npArtist to (artist of npTrack) as text
  end try
  set npAlbum to ""
  try
    set npAlbum to (album of npTrack) as text
  end try
  set npAlbumArtist to ""
  try
    set npAlbumArtist to (album artist of npTrack) as text
  end try
  set npDuration to 0
  try
    set npDuration to (duration of npTrack)
  end try
  set npPosition to 0
  try
    set npPosition to (player position)
  end try
  set npTrackNumber to 0
  try
    set npTrackNumber to (track number of npTrack)
  end try
  set npOut to npState & linefeed & npName & linefeed & npArtist
  set npOut to npOut & linefeed & npAlbum & linefeed & npAlbumArtist
  set npOut to npOut & linefeed & (npDuration as text) & linefeed & (npPosition as text)
  set npOut to npOut & linefeed & (npTrackNumber as text)
  return npOut
end tell
"""

# Spotify reports duration in MILLISECONDS while position is in seconds. Mixing
# those up is the classic bug in every Spotify-on-Mac integration.
SPOTIFY_SCRIPT = """
if application "Spotify" is not running then return ""
tell application "Spotify"
  set npState to (player state as text)
  if npState is "stopped" then return "stopped"
  try
    set npTrack to current track
  on error
    return "stopped"
  end try
  set npName to ""
  try
    set npName to (name of npTrack) as text
  end try
  if npName is "" then return "stopped"
  set npArtist to ""
  try
    set npArtist to (artist of npTrack) as text
  end try
  set npAlbum to ""
  try
    set npAlbum to (album of npTrack) as text
  end try
  set npAlbumArtist to ""
  try
    set npAlbumArtist to (album artist of npTrack) as text
  end try
  set npDuration to 0
  try
    set npDuration to (duration of npTrack)
  end try
  set npPosition to 0
  try
    set npPosition to (player position)
  end try
  set npTrackNumber to 0
  try
    set npTrackNumber to (track number of npTrack)
  end try
  set npArtwork to ""
  try
    set npArtwork to (artwork url of npTrack) as text
  end try
  set npOut to npState & linefeed & npName & linefeed & npArtist
  set npOut to npOut & linefeed & npAlbum & linefeed & npAlbumArtist
  set npOut to npOut & linefeed & (npDuration as text) & linefeed & (npPosition as text)
  set npOut to npOut & linefeed & (npTrackNumber as text) & linefeed & npArtwork
  return npOut
end tell
"""

ADAPTERS = [
    # source id doubles as the filter token, and matches the Windows bridge's
    # vocabulary so one widget setting works on both platforms.
    {"source": "applemusic", "name": "Apple Music", "script": MUSIC_SCRIPT,
     "duration_ms": False, "art": "raw"},
    {"source": "spotify", "name": "Spotify", "script": SPOTIFY_SCRIPT,
     "duration_ms": True, "art": "url"},
]

STATUS_MAP = {"playing": "playing", "paused": "paused", "stopped": "stopped"}


def parse_fields(raw, adapter):
    """Turn one adapter's delimited reply into a session dict, or None."""
    if not raw or raw == "stopped":
        return None
    parts = raw.split(SEP)
    if len(parts) < 8:
        return None
    state, title, artist, album, album_artist, dur_s, pos_s, trk = [p.strip() for p in parts[:8]]
    title = title.strip()
    if not title:
        return None

    def num(text):
        try:
            return float(str(text).replace(",", "."))
        except ValueError:
            return 0.0

    duration = num(dur_s)
    duration_ms = int(duration if adapter["duration_ms"] else duration * 1000)
    position_ms = int(num(pos_s) * 1000)
    if duration_ms and position_ms > duration_ms:
        position_ms = duration_ms
    now_ms = int(time.time() * 1000)
    status = STATUS_MAP.get(state.strip().lower(), "unknown")

    return {
        "source": adapter["source"],
        "source_name": adapter["name"],
        "title": title,
        "artist": artist.strip(),
        "album": album.strip(),
        "album_artist": album_artist.strip(),
        "track_number": int(num(trk)),
        "album_track_count": 0,
        "subtitle": "",
        "genres": [],
        "status": status,
        "is_playing": status == "playing",
        "playback_type": "music",
        "is_shuffle_active": False,
        "auto_repeat_mode": "none",
        "duration_ms": duration_ms,
        "position_ms": position_ms,
        # AppleScript reports position at the instant it is asked, so the
        # snapshot IS current — no extrapolation from a stale timestamp needed.
        "position_at": now_ms,
        "position_snapshot_ms": position_ms,
        "position_updated_at": now_ms,
        "timeline_ok": duration_ms > 0,
        "thumbnail": None,
        "_artwork_url": parts[8].strip() if len(parts) > 8 else "",
    }


# Artwork is expensive on the Music.app path (AppleScript writes the bytes to a
# temp file), so keep the last few and only re-read on a track change.
_art_cache = {}
_ART_CACHE_MAX = 8
ART_TMP = "/tmp/nowplaying-bridge-art"

ART_SCRIPT = """
if application "Music" is not running then return ""
tell application "Music"
  try
    set npData to raw data of artwork 1 of current track
  on error
    return ""
  end try
end tell
try
  set npFile to open for access (POSIX file "%s") with write permission
  set eof npFile to 0
  write npData to npFile
  close access npFile
on error
  try
    close access (POSIX file "%s")
  end try
  return ""
end try
return "ok"
""" % (ART_TMP, ART_TMP)


def sniff_mime(head):
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return ""


def music_artwork():
    """Pull Music.app's embedded cover out via a temp file, as a data URL."""
    out, err = run_applescript(ART_SCRIPT, timeout=6.0)
    if err or out != "ok":
        return None
    try:
        with open(ART_TMP, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    finally:
        try:
            os.remove(ART_TMP)
        except OSError:
            pass
    if len(raw) < 256:
        return None
    mime = sniff_mime(raw[:8])
    if not mime:
        return None
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def fetch_art_url(url):
    """
    Download a player's artwork URL and return it as a data URL.

    Spotify's `artwork url` is a short-lived CDN link: it loads once and then
    stops resolving, which showed up as album art that flashed and vanished.
    Fetching the bytes here once and handing back a data URL makes the artwork
    permanent for that track, and matches what the Windows bridge always sends.
    """
    if not url.startswith(("http://", "https://")):
        return None  # older Spotify builds return an `image:` handle we can't read
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024)
    except Exception:
        return None
    if len(raw) < 256:
        return None
    mime = sniff_mime(raw[:8])
    if not mime:
        return None
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def resolve_artwork(session, adapter):
    """
    Attach artwork, cached per track.

    A miss is cached too. Some Apple Music cloud tracks simply have no local
    artwork, and retrying every half second for a whole song would launch a
    hundred pointless AppleScript processes. The widget looks the cover up by
    name when the bridge hands over nothing, so a miss costs the user nothing.
    """
    key = (session["source"], session["title"], session["artist"])
    if key in _art_cache:
        return _art_cache[key]
    art = None
    if adapter["art"] == "url" and session.get("_artwork_url"):
        # Fetch rather than pass the URL through: see fetch_art_url.
        art = fetch_art_url(session["_artwork_url"])
    elif adapter["art"] == "raw":
        art = music_artwork()
    if len(_art_cache) >= _ART_CACHE_MAX:
        _art_cache.clear()
    _art_cache[key] = art
    return art


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------
state = {"sessions": [], "current": None, "updated_at": 0, "error": None}
_lock = threading.Lock()
_stop = threading.Event()

GRACE_MS = 4000
_last_good = None


def set_state(**kwargs):
    with _lock:
        state.update(kwargs)


def get_state():
    with _lock:
        return dict(state)


def is_showable(session):
    if not session or not session.get("title"):
        return False
    return session["status"] not in ("stopped", "unknown")


def pick_current(sessions):
    """A session that is actually playing wins over one merely open and paused."""
    playing = [s for s in sessions if s["is_playing"]]
    if playing:
        return playing[0]
    return sessions[0] if sessions else None


def poll_once():
    global _last_good
    found, permission_denied, other_error = [], False, None

    for adapter in ADAPTERS:
        raw, err = run_applescript(adapter["script"])
        if err == "permission":
            permission_denied = True
            continue
        if err:
            # A real AppleScript fault, not just "nothing loaded". Surface it:
            # a syntax or dictionary error is a bug I need to see, and silently
            # showing "nothing playing" would hide it.
            other_error = f"{adapter['name']}: {err}"
            continue
        session = parse_fields(raw, adapter)
        if session:
            session["thumbnail"] = resolve_artwork(session, adapter)
            session.pop("_artwork_url", None)
            found.append(session)

    if permission_denied:
        set_state(
            sessions=[], current=None, updated_at=int(time.time() * 1000),
            error=("Automation permission was refused. Open System Settings → "
                   "Privacy & Security → Automation and allow NowPlaying Bridge "
                   "to control Music and Spotify."),
        )
        return

    current = pick_current(found)
    now_wall = time.time()
    if is_showable(current):
        _last_good = {"session": current, "at": now_wall}
    elif _last_good and (now_wall - _last_good["at"]) * 1000 < GRACE_MS:
        # Players briefly report nothing between tracks; hold the last good one
        # so overlays don't blink on every skip.
        current = _last_good["session"]
        if current["source"] not in {s["source"] for s in found}:
            found = found + [current]

    set_state(
        sessions=found, current=current,
        updated_at=int(time.time() * 1000),
        error=other_error,
    )


def poll_forever():
    last_key = None
    while not _stop.is_set():
        try:
            poll_once()
            if not QUIET:
                cur = get_state()["current"]
                key = (cur or {}).get("title"), (cur or {}).get("artist")
                if cur and key != last_key:
                    safe_print(f"  ♪ {cur['artist']} — {cur['title']}  [{cur['source_name']}]")
                    last_key = key
        except Exception as exc:
            set_state(error=str(exc))
        _stop.wait(POLL_SECONDS)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME.replace(' ', '')}/{VERSION}"

    def log_message(self, *args):
        pass  # a request log per poll is noise, not information

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        snapshot = get_state()

        if route in ("/", "/now-playing"):
            wanted = (query.get("app") or [""])[0].strip().lower()
            session = snapshot["current"]
            if wanted:
                matches = [
                    s for s in snapshot["sessions"]
                    if wanted in f"{s['source']} {s['source_name']}".lower()
                ]
                session = next((s for s in matches if s["is_playing"]), None) or (
                    matches[0] if matches else None
                )
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "platform": "macos",
                "updated_at": snapshot["updated_at"],
                "session": self._live(session) if is_showable(session) else None,
            })
            return

        if route == "/sessions":
            if "text/html" in (self.headers.get("Accept") or ""):
                self._send_sessions_page(snapshot)
                return
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "sessions": [
                    {k: s[k] for k in
                     ("source", "source_name", "status", "playback_type", "title", "artist")}
                    for s in snapshot["sessions"]
                ],
            })
            return

        if route == "/health":
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "platform": "macos",
                "macos": mac_version(),
                "ok": snapshot["error"] is None,
                "error": snapshot["error"],
                "sessions": len(snapshot["sessions"]),
                "updated_at": snapshot["updated_at"],
            })
            return

        self._send({"error": "not found", "routes": ["/now-playing", "/sessions", "/health"]}, 404)

    def _live(self, session):
        """Advance position to this instant, so every reply is current."""
        if not session:
            return None
        out = dict(session)
        now_ms = int(time.time() * 1000)
        out["position_at"] = now_ms
        if out.get("timeline_ok") and out.get("is_playing"):
            pos = out["position_snapshot_ms"] + max(0, now_ms - out["position_updated_at"])
            out["position_ms"] = min(pos, out["duration_ms"]) if out["duration_ms"] else pos
        return out

    def _send(self, payload, status=200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # Overlays load from file:// or a stream tool, so their origin is opaque.
        # Localhost-only, read-only data, so a wildcard is safe here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _send_sessions_page(self, snapshot):
        def esc(t):
            return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        rows = []
        for s in snapshot["sessions"]:
            track = " — ".join(x for x in (s["artist"], s["title"]) if x) or "nothing loaded"
            rows.append(
                f"<li><code>{esc(s['source'])}</code>"
                f"<div class=meta>{esc(s['source_name'])} · {esc(s['status'])}</div>"
                f"<div class=track>{esc(track)}</div></li>"
            )
        if not rows:
            note = snapshot["error"] or ("Nothing is playing in Music or Spotify. "
                                        "Press play and reload this page.")
            rows.append(f"<li class=empty>{esc(note)}</li>")
        body = """<!doctype html><html><head><meta charset=utf-8>
<title>{app} — active players</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{{margin:0;background:#0e0e12;color:#f2f2f5;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px}}
h1{{font-size:19px;margin:0 0 4px}}
p{{color:#8b8b97;margin:0 0 22px;font-size:13.5px;max-width:60ch}}
ul{{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px;max-width:70ch}}
li{{background:#16161c;border:1px solid #26262e;border-radius:12px;padding:14px 16px}}
li.empty{{color:#8b8b97}}
code{{font:13px/1.5 ui-monospace,Menlo,monospace;color:#ffd479}}
.meta{{color:#8b8b97;font-size:12px;margin-top:6px;text-transform:uppercase;letter-spacing:.06em}}
.track{{margin-top:6px;font-size:14px}}
footer{{color:#8b8b97;font-size:12.5px;margin-top:24px}}
</style></head><body>
<h1>Active players</h1>
<p>Copy the highlighted id into the widget's “Follow only” field to pin the
overlay to one player. Music playing in a browser tab cannot appear here —
browsers expose no way to read it. Use the Last.fm route for those.</p>
<ul>{rows}</ul>
<footer>{app} {ver} · reload to refresh</footer>
</body></html>""".format(app=APP_NAME, ver=VERSION, rows="".join(rows))
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def show_error(title, message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        safe_print(f"{title}: {message}")


def main():
    ini_host, ini_port = load_settings()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=None, help=f"overrides settings.ini (default {ini_port})")
    parser.add_argument("--host", default=None, help=f"overrides settings.ini (default {ini_host})")
    parser.add_argument("--console", action="store_true", help="no window, log to the terminal")
    parser.add_argument("--quiet", action="store_true", help="don't print track changes")
    args = parser.parse_args()
    args.host = args.host or ini_host
    args.port = args.port or ini_port

    if sys.platform != "darwin":
        safe_print(f"{APP_NAME} for macOS needs macOS. On Windows use NowPlayingBridge.exe.")
        return 1

    if args.quiet:
        global QUIET
        QUIET = True

    if already_running(args.host, args.port):
        msg = (f"{APP_NAME} is already running.\n\n"
               "Look for its window, or its icon in the Dock. "
               "You only need one copy open.")
        if args.console:
            safe_print("  " + msg.replace("\n", "\n  "))
        else:
            show_error(APP_NAME, msg)
        return 0

    threading.Thread(target=poll_forever, name="player-poller", daemon=True).start()
    url = f"http://{args.host}:{args.port}/now-playing"

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        msg = (f"Could not start on port {args.port}.\n\n{exc}\n\n"
               "Another program is using it. Open settings.ini next to this app "
               "and change the port to 5789, then start it again.")
        if args.console:
            safe_print("  " + msg.replace("\n", "\n  "))
        else:
            show_error(f"{APP_NAME} — could not start", msg)
        return 1

    serve = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    serve.start()

    if args.console:
        safe_print("")
        safe_print(f"  {APP_NAME} {VERSION}")
        safe_print(f"  {url}")
        safe_print("")
        safe_print("  Reads Music and Spotify. Leave this open while you stream.")
        safe_print("")
        try:
            serve.join()
        except KeyboardInterrupt:
            safe_print("\n  Stopped.")
        finally:
            _stop.set()
            server.server_close()
        return 0

    try:
        from status_window import StatusWindow
        StatusWindow(APP_NAME, VERSION, url, get_state, server.shutdown).run()
    except Exception as exc:
        safe_print(f"  Could not open the window ({exc}). Running in console mode.")
        safe_print(f"  {url}")
        try:
            serve.join()
        except KeyboardInterrupt:
            pass
    finally:
        _stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last resort before exit
        log_crash(exc)
        show_error(
            f"{APP_NAME} stopped",
            "Something went wrong and the bridge had to close.\n\n"
            'A file was saved in the "logs" folder next to the app. '
            "Send me the newest one and I'll fix it.\n\n"
            f"{exc}",
        )
        sys.exit(1)
