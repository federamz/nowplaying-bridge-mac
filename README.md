# NowPlaying Bridge for macOS

Reads the track playing on your Mac and serves it as JSON on localhost, so a
stream overlay can display it. Companion to the Windows bridge; **same port, same
JSON, same field names** — a widget cannot tell them apart and needs no setting
changed.

MIT licensed. Standard library only: no pip install, no third-party runtime.

## Quick start

1. Download `NowPlayingBridge-mac-arm64.zip` (Apple Silicon) or
   `-x86_64.zip` (Intel) from the [latest release](../../releases/latest).
2. Unzip and drag **NowPlaying Bridge** into your Applications folder.
3. Open it. macOS will warn you the first time — see *Gatekeeper* below.
4. The first time it reads a player, macOS asks for permission to control Music
   and Spotify. **Allow it** — that is how it reads your music.
5. Press play. The window names the track; the overlay picks it up.

Options: `--port 5789`, `--host 0.0.0.0`, `--console` (terminal instead of a
window), `--quiet`. Settings also live in `settings.ini` next to the app.

## What it can and cannot read

| | |
| :--- | :--- |
| **Music.app** (Apple Music, local library, iTunes purchases) | Full metadata, embedded album art, **exact** playback position |
| **Spotify.app** | Full metadata, artwork URL, exact position |
| Anything in a **browser tab** | Not readable. See below. |
| Other desktop players | Only if they ship an AppleScript dictionary. Easy to add — see *Adding a player*. |

**Why browsers don't work.** macOS has a system-wide now-playing service
(MediaRemote) that Control Center reads. From **macOS 15.4** Apple gated it
behind a private entitlement, and every third-party tool built on it stopped
working. The workarounds are code injection with SIP disabled — unacceptable for
software people pay for — or asking each app directly through AppleScript, which
is public, supported, and needs no security changes. This bridge does the latter.
Browsers expose no scripting interface for media, so a tab playing YouTube Music
or Apple Music on the web is invisible to it.

For those, use the widget's **Last.fm route** with the Web Scrobbler extension.
Slower (about ten seconds) and the progress bar is estimated, but it works with
anything.

**One thing macOS does better than Windows:** AppleScript reports the playback
position at the instant it is asked, so the progress bar is exact. The Windows
API only offers a snapshot with a timestamp, which has to be extrapolated.

## Gatekeeper: "cannot be opened because Apple cannot check it"

Expected. The app is not notarised, which needs a paid Apple Developer
membership. Nothing is wrong with the file.

To open it: **right-click the app → Open**, then confirm. If that option does
nothing, go to **System Settings → Privacy & Security**, scroll to the message
about NowPlaying Bridge, and click **Open Anyway**.

Only needed once per version. The source is here; build it yourself with
`build.command` if you would rather not trust a download.

## Automation permission

The first read triggers *"NowPlaying Bridge wants access to control Music."*
Allow it. If you refuse, or clicked past it, the bridge says so plainly in its
window and at `/health`, and you can fix it in **System Settings → Privacy &
Security → Automation**.

The build is ad-hoc signed, which gives it a stable identity — so the permission
you grant survives updates instead of re-prompting every time.

## API

`GET http://127.0.0.1:5788/now-playing` — optional `?app=` filter
(`applemusic`, `spotify`).

```json
{
  "bridge": "NowPlaying Bridge",
  "version": "1.0.0-mac",
  "platform": "macos",
  "updated_at": 1785300000123,
  "session": {
    "source": "applemusic",
    "source_name": "Apple Music",
    "title": "Fast Car",
    "artist": "Tracy Chapman",
    "album": "Tracy Chapman",
    "album_artist": "Tracy Chapman",
    "track_number": 1,
    "status": "playing",
    "is_playing": true,
    "playback_type": "music",
    "duration_ms": 296000,
    "position_ms": 63120,
    "position_at": 1785300000123,
    "position_snapshot_ms": 63120,
    "position_updated_at": 1785300000123,
    "timeline_ok": true,
    "thumbnail": "data:image/jpeg;base64,..."
  }
}
```

`session` is `null` when nothing is loaded or playback stopped — the overlay's
cue to hide. A pause keeps the session with `status: "paused"`.

`thumbnail` is a data URL for Music.app, an `https://` URL for Spotify, and
`null` when the player has no artwork. A client that gets `null` should look the
cover up by artist and title.

`GET /sessions` — every player it can see. Open it in a browser for a readable
page listing the id to paste into an overlay's app filter.

`GET /health` — version, macOS version, and any permission error in plain words.

## Adding a player

`ADAPTERS` near the top of `nowplaying_bridge_mac.py` is a list. Each entry is an
AppleScript that returns one delimited line, plus two flags. Copy the Spotify
entry, change the app name, and check its dictionary in Script Editor
(**File → Open Dictionary**) for the field names.

Two rules learned the hard way:

- Guard every script with `if application "X" is not running then return ""`.
  A plain `tell application "Music"` **launches** Music — an overlay that opened
  iTunes because someone loaded a browser source would be a disaster.
- Check the units. Music.app reports duration in **seconds**; Spotify reports it
  in **milliseconds**. That mismatch is the classic bug in every Spotify
  integration.

## Notes

If something goes wrong the app writes a timestamped file into a `logs` folder
beside it and keeps the newest ten. Starting a second copy is detected and says
so instead of failing on the port.

Players briefly report nothing between tracks, so the bridge holds the last good
session for 4 seconds to stop overlays blinking on every skip.

Album artwork is cached per track, misses included — some Apple Music cloud
tracks have no local artwork, and retrying twice a second for a whole song would
spawn hundreds of pointless processes.
