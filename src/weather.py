import logging
from datetime import datetime
from urllib.parse import urlencode
from src import config
from src.http_client import request_json

LOGGER = logging.getLogger(__name__)


def get_mashhad_weather():
    if not config.OPENWEATHER_API_KEY:
        LOGGER.info("Weather skipped: OPENWEATHER_API_KEY is not configured")
        return None
    query = urlencode({"lat": config.MASHHAD_LAT, "lon": config.MASHHAD_LON,
                       "units": "metric", "lang": "fa", "appid": config.OPENWEATHER_API_KEY})
    base = "https://api.openweathermap.org/data/2.5/"
    try:
        current = request_json(f"{base}weather?{query}")
        info = {"current": {"temp": float(current["main"]["temp"]),
                            "feels_like": current["main"].get("feels_like"),
                            "humidity": int(current["main"]["humidity"]),
                            "description": current["weather"][0]["description"],
                            "wind_speed": float(current["wind"]["speed"])}, "forecast": []}
    except Exception as exc:
        LOGGER.warning("Current weather unavailable (%s)", type(exc).__name__)
        return None
    try:
        forecast = request_json(f"{base}forecast?{query}")
        now = datetime.now(config.LOCAL_TZ)
        for item in forecast.get("list", []):
            local = datetime.fromtimestamp(item["dt"], config.LOCAL_TZ)
            if local <= now:
                continue
            info["forecast"].append(
                f"{local:%m/%d، %H:%M} · {item['main']['temp']:g}°C · {item['weather'][0]['description']}")
            if len(info["forecast"]) == 4:
                break
    except Exception as exc:
        LOGGER.warning("Weather forecast unavailable (%s)", type(exc).__name__)
    return info
