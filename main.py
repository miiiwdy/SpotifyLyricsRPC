import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

from pypresence import Presence
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

config = "config.json"

def watermark():
    print("""
====================================
   SpotifyLyricsRPC by sn33vilz
   github.com/miiiwdy
====================================
""")

def load_discord_id() -> str:
    if os.path.exists(config):
        try:
            with open(config, "r", encoding="utf-8") as f:
                data = json.load(f)

            app_id = str(data.get("discord_app_id", "")).strip()

            if app_id:
                return app_id

        except Exception:
            pass

    while True:
        app_id = input("Enter Discord Application ID: ").strip()

        if not app_id:
            print("Application ID cannot be empty")
            continue

        if not app_id.isdigit():
            print("Application ID must be numeric")
            continue

        break

    with open(config, "w", encoding="utf-8") as f:
        json.dump(
            {
                "discord_app_id": app_id
            },
            f,
            indent=2
        )
        print("Config saved")

    return app_id

DiscordID = load_discord_id()
Interval: float = 0.1
LyricOffsetMilisecond: int = 700

@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    position_ms: int
    duration_ms: int
    playing: bool
    fetched_at: float

    @property
    def key(self) -> str:
        return f"{self.title}:{self.artist}"

    @property
    def label(self) -> str:
        return f"{self.title} - {self.artist}"

    def realtime_position_ms(self) -> int:
        if not self.playing:
            return self.position_ms

        elapsed = (time.monotonic() - self.fetched_at) * 1000
        return min(
            self.duration_ms,
            max(0, int(self.position_ms + elapsed)),
        )

@dataclass
class LyricLine:
    time_ms: int
    text: str


async def current_track() -> Optional[Track]:
    try:
        manager = await MediaManager.request_async()
        session = manager.get_current_session()

        if session is None:
            return None

        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
        duration = int(timeline.end_time.total_seconds() * 1000)

        if duration <= 0:
            return None

        return Track(
            title=str(props.title).strip(),
            artist=str(props.artist).strip(),
            position_ms=max(0, int(timeline.position.total_seconds() * 1000)),
            duration_ms=duration,
            playing=playback.playback_status == PlaybackStatus.PLAYING,
            fetched_at=time.monotonic(),
        )
    except Exception:
        return None


def fetch_lyrics(title: str, artist: str) -> Optional[list[LyricLine]]:
    params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    try:
        with urllib.request.urlopen(
            f"https://lrclib.net/api/get?{params}", timeout=5
        ) as resp:
            raw = json.loads(resp.read()).get("syncedLyrics")
    except Exception:
        return None

    if not raw:
        return None

    lines: list[LyricLine] = []

    for line in raw.splitlines():
        if not line.startswith("["):
            continue
        bracket, _, text = line.partition("]")
        text = text.strip()
        if not text:
            continue
        try:
            minutes, seconds = bracket.lstrip("[").split(":")
            time_ms = int(float(minutes) * 60_000 + float(seconds) * 1_000)
            lines.append(LyricLine(time_ms, text))
        except ValueError:
            continue

    return sorted(lines, key=lambda x: x.time_ms)


def active_line(lyrics: list[LyricLine], position_ms: int) -> str:
    timestamps = [l.time_ms for l in lyrics]
    index = bisect_right(timestamps, position_ms) - 1
    return lyrics[index].text if index >= 0 else ""

class App:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._rpc = Presence(DiscordID)
        self._rpc.connect()
        self._rpc_ready = True

        self._cache: dict[str, list[LyricLine]] = {}
        self._last_key = ""
        self._last_lyric = ""
        self._next_retry = 0.0

    def run(self) -> None:
        watermark()
        try:
            while True:
                self._tick()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _tick(self) -> None:
        track = self._loop.run_until_complete(current_track())

        if track is None:
            self._clear()
            self._last_key = ""
            self._last_lyric = ""
            time.sleep(1)
            return

        if not track.playing:
            time.sleep(0.5)
            return

        if track.key != self._last_key:
            print(f"playing: {track.label}")
            self._last_key = track.key
            self._last_lyric = ""

        lyrics = self._cache.get(track.key)
        if lyrics is None:
            fetched = fetch_lyrics(track.title, track.artist)
            lyrics = self._cache[track.key] = fetched or []

        lyric = (
            active_line(lyrics, track.realtime_position_ms() + LyricOffsetMilisecond)
            if lyrics else track.title
        )

        if lyric != self._last_lyric:
            self._update(track, lyric)
            self._last_lyric = lyric

        time.sleep(Interval)

    def _update(self, track: Track, state: str) -> None:
        if not self._rpc_ready:
            self._reconnect()
            return

        now = int(time.time())
        try:
            self._rpc.update(
                details=track.label[:128],
                state=state[:128],
                large_image="spotify",
                large_text="SpotifyLyricsRPC by sneevilz",
                start=now - track.position_ms // 1_000,
                end=now + (track.duration_ms - track.position_ms) // 1_000,
            )
        except Exception:
            self._rpc_ready = False
            print("Discord RPC disconnected")

    def _clear(self) -> None:
        if self._rpc_ready:
            try:
                self._rpc.clear()
            except Exception:
                self._rpc_ready = False
                print("Discord RPC disconnected")

    def _reconnect(self) -> None:
        if time.time() < self._next_retry:
            return
        self._next_retry = time.time() + 5
        try:
            self._rpc.close()
            self._rpc = Presence(DiscordID)
            self._rpc.connect()
            self._rpc_ready = True
        except Exception:
            pass

    def _shutdown(self) -> None:
        self._clear()
        try:
            self._rpc.close()
        except Exception:
            pass
        self._loop.close()


if __name__ == "__main__":
    App().run()