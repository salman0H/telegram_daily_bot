"""Environment configuration. Importing modules never requires live credentials."""
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _env(name, default=""):
    value = os.getenv(name, default).strip()
    return "" if value.lower() in {"none", "null"} else value


def _integer(name, default, minimum, maximum):
    try:
        value = int(_env(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


BOT_ENABLED = _env("BOT_ENABLED", "true").lower() != "false"
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = _env("TELEGRAM_CHANNEL_ID")
TELEGRAM_USER_ID = _env("TELEGRAM_USER_ID")
MEMBERSHIP_CHANNEL_ID = _env("MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
CHANNEL_JOIN_URL = _env("CHANNEL_JOIN_URL")
OPENWEATHER_API_KEY = _env("OPENWEATHER_API_KEY")
GROQ_API_KEY = _env("GROQ_API_KEY")
SPOTIFY_CLIENT_ID = _env("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _env("SPOTIFY_CLIENT_SECRET")
SPORTRADAR_SOCCER_API_KEY = _env("SPORTRADAR_SOCCER_API_KEY")
SPORTRADAR_BASKETBALL_API_KEY = _env("SPORTRADAR_BASKETBALL_API_KEY")
TIMEZONE = _env("TIMEZONE", "Asia/Tehran")
try:
    LOCAL_TZ = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError as exc:
    raise ValueError("TIMEZONE must be an installed IANA timezone, e.g. Asia/Tehran") from exc
CITY_NAME = _env("CITY_NAME", "مشهد")
MASHHAD_LAT = float(_env("WEATHER_LAT", "36.2605"))
MASHHAD_LON = float(_env("WEATHER_LON", "59.6168"))
if not -90 <= MASHHAD_LAT <= 90 or not -180 <= MASHHAD_LON <= 180:
    raise ValueError("Weather coordinates are out of range")
NEWS_MAX_AGE_HOURS = _integer("NEWS_MAX_AGE_HOURS", 36, 1, 168)
NEWS_PER_CATEGORY = _integer("NEWS_PER_CATEGORY", 2, 1, 3)
GROQ_MODELS = [m.strip() for m in _env(
    "GROQ_MODELS", "openai/gpt-oss-120b,llama-3.3-70b-versatile"
).split(",") if m.strip()]
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
ENABLE_VOCABULARY = _env("ENABLE_VOCABULARY", "true").lower() == "true"
ENABLE_VOCABULARY_DICTIONARY = _env("ENABLE_VOCABULARY_DICTIONARY", "true").lower() == "true"
VOCABULARY_PER_DAY = _integer("VOCABULARY_PER_DAY", 5, 1, 8)
ENABLE_PERSIAN_CALENDAR = _env("ENABLE_PERSIAN_CALENDAR", "true").lower() == "true"
ENABLE_PERSIAN_POETRY = _env("ENABLE_PERSIAN_POETRY", "true").lower() == "true"
ENABLE_AUDIO_DOWNLOAD = _env("ENABLE_AUDIO_DOWNLOAD", "false").lower() == "true"


def validate(for_delivery=True):
    if for_delivery and not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is required for delivery")
    if for_delivery and not (TELEGRAM_CHANNEL_ID or TELEGRAM_USER_ID):
        raise ValueError("Set TELEGRAM_CHANNEL_ID or TELEGRAM_USER_ID for delivery")
