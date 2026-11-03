"""Music discovery. Downloads are optional and confined to a temporary directory."""
import base64
import logging
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from src import config
from src.http_client import request_json
from src.news import safe_url
from src.writer import plain_field

LOGGER = logging.getLogger(__name__)


def get_trending_song():
    """Return a track from a new release (not a claim about chart position)."""
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        return None
    try:
        auth = base64.b64encode(f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
        token = request_json("https://accounts.spotify.com/api/token",
                             data=urlencode({"grant_type": "client_credentials"}).encode(),
                             headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        albums = request_json("https://api.spotify.com/v1/browse/new-releases?limit=1", headers=headers)
        album = albums["albums"]["items"][0]
        album_id = album["id"]
        tracks = request_json(f"https://api.spotify.com/v1/albums/{album_id}/tracks?limit=1", headers=headers)
        track = tracks["items"][0]
        link = safe_url(track.get("external_urls", {}).get("spotify", ""))
        if not link:
            return None
        image_url = next((url for image in (album.get("images") or [])
                          if isinstance(image, dict) and (url := safe_url(image.get("url", "")))), "")
        return {"title": plain_field(track["name"], 160),
                "artist": plain_field(", ".join(artist["name"] for artist in track["artists"]), 160), "link": link, "image_url": image_url}
    except Exception as exc:
        LOGGER.warning("Spotify discovery unavailable (%s)", type(exc).__name__)
        return None


def download_audio(query, output_dir):
    if not config.ENABLE_AUDIO_DOWNLOAD or not query:
        return None
    path = Path(output_dir) / "song.mp3"
    command = ["yt-dlp", "--no-playlist", "--extract-audio", "--audio-format", "mp3",
               "--max-filesize", "45M", "--match-filter", "duration <= 600",
               "--socket-timeout", "15", "--retries", "2", "--force-ipv4",
               "-o", str(Path(output_dir) / "song.%(ext)s"), f"ytsearch1:{query} audio"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode == 0 and path.is_file() and 0 < path.stat().st_size <= 45 * 1024 * 1024:
            return str(path)
        LOGGER.warning("Audio download did not produce a usable file")
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("Audio download unavailable (%s)", type(exc).__name__)
    return None


def get_apple_music_cards():
    """Pair each US iTunes chart entry with its own publisher-provided artwork."""
    try:
        data = request_json("https://itunes.apple.com/us/rss/topsongs/limit=5/json")
        feed = data.get("feed", {})
        updated = plain_field(feed.get("updated", {}).get("label", ""), 80)
        cards = []
        for index, entry in enumerate(feed.get("entry", [])[:5], 1):
            title = plain_field(entry.get("im:name", {}).get("label", ""), 160)
            artist = plain_field(entry.get("im:artist", {}).get("label", ""), 160)
            link = safe_url(entry.get("id", {}).get("label", ""))
            if not title or not link:
                continue
            images = entry.get("im:image", [])
            if not isinstance(images, list):
                images = []
            def height(image):
                try:
                    return int(image.get("attributes", {}).get("height", 0))
                except (ValueError, TypeError, AttributeError):
                    return 0
            image_url = next((url for image in sorted(images, key=height, reverse=True)
                              if isinstance(image, dict) and (url := safe_url(image.get("label", "")))), "")
            text = (f"🎧 **چارت فروشگاه iTunes آمریکا · رتبهٔ {index}**\n\n"
                    f"**{title}**\n{artist}\n{link}")
            if updated:
                text += f"\nبه‌روزرسانی منبع: {updated}"
            cards.append({"text": text, "image_url": image_url})
        return cards
    except Exception as exc:
        LOGGER.warning("iTunes chart unavailable (%s)", type(exc).__name__)
        return []


def get_apple_music_top_5():
    """Text-only chart view for callers that do not handle artwork."""
    cards = get_apple_music_cards()
    return "\n\n".join(card["text"] for card in cards) if cards else None
