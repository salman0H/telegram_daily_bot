"""Optional, no-key Iranian APIs discovered through the supplied resource lists."""
from datetime import datetime
import logging
from urllib.parse import urlencode
from src import config
from src.http_client import request_json
from src.persian import local_stamp
from src.news import safe_url
from src.writer import plain_field

LOGGER = logging.getLogger(__name__)


def get_calendar_card(now=None):
    if not config.ENABLE_PERSIAN_CALENDAR:
        return None
    now = (now or datetime.now(config.LOCAL_TZ)).astimezone(config.LOCAL_TZ)
    heading = f"📅 **تقویم امروز**\n{local_stamp(now)} ({config.TIMEZONE})"
    try:
        url = f"https://holidayapi.ir/gregorian/{now.year}/{now.month}/{now.day}"
        data = request_json(url, timeout=8, attempts=1)
        if not isinstance(data, dict) or not isinstance(data.get("is_holiday"), bool) or not isinstance(data.get("events"), list):
            raise ValueError("Invalid calendar response")
        events, seen = [], set()
        for event in data["events"]:
            if not isinstance(event, dict):
                continue
            description = plain_field(event.get("description", ""), 240)
            if description and description not in seen:
                events.append(f"▫️ {description}")
                seen.add(description)
            if len(events) == 4:
                break
        status = "تعطیل طبق منبع" if data["is_holiday"] else "تعطیلی در منبع ثبت نشده"
        details = "\n".join(events) if events else "مناسبتی در پاسخ منبع ثبت نشده است."
        return {"text": f"{heading}\n\n{status}\n{details}\n\nمنبع: holidayapi.ir (داده‌های time.ir)\nhttps://holidayapi.ir", "image_url": ""}
    except Exception as exc:
        LOGGER.warning("Calendar events unavailable (%s)", type(exc).__name__)
        return {"text": f"{heading}\n\nاطلاعات مناسبت و تعطیلی در دسترس نیست.", "image_url": ""}


def daily_hafez_path(now=None):
    """A shuffled 495-day cycle, determined solely by the configured local day."""
    day = (now or datetime.now(config.LOCAL_TZ)).astimezone(config.LOCAL_TZ).date()
    # 137 is coprime to Ganjoor's 495 ghazals: no repeats within a full cycle.
    number = (day.toordinal() * 137 + 41) % 495 + 1
    return f"/hafez/ghazal/sh{number}"


def get_poetry_card(now=None):
    """Daily shared Hafez fal: original full ghazal, without invented predictions."""
    if not config.ENABLE_PERSIAN_POETRY:
        return None
    now = (now or datetime.now(config.LOCAL_TZ)).astimezone(config.LOCAL_TZ)
    expected_path = daily_hafez_path(now)
    try:
        page = request_json("https://api.ganjoor.net/api/ganjoor/page?" + urlencode({"url": expected_path}),
                            timeout=8, attempts=1)
        data = page.get("poem") if isinstance(page, dict) else None
        if not isinstance(data, dict):
            raise ValueError("Invalid poetry response")
        title = plain_field(data.get("fullTitle", ""), 180)
        path = data.get("fullUrl", "")
        if path != expected_path:
            raise ValueError("Unexpected daily ghazal")
        source_url = safe_url("https://ganjoor.net" + path)
        raw_text = data.get("plainText")
        if not source_url or not isinstance(raw_text, str) or not title.startswith("حافظ"):
            raise ValueError("Missing Hafez attribution")
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not 2 <= len(lines) <= 100 or len(lines) % 2 or any(len(line) > 240 for line in lines):
            raise ValueError("Incomplete or oversized ghazal")
        # Keep all couplets and pair their lines. Never use the API's AI summary.
        pairs = ["\n".join(plain_field(line, 240) for line in lines[index:index + 2])
                 for index in range(0, len(lines), 2)]
        verse = "\n\n".join(pairs)
        if len(verse) > 12000:
            raise ValueError("Ghazal exceeds report size budget")
        return {"text": f"📜 **فال حافظ روزانه**\n{local_stamp(now).split(' · ')[0]}\n\n"
                        f"نیت کنید و غزل امروز را بخوانید.\n\n**{title}**\n\n{verse}\n\n"
                        f"آیینی ادبی برای تأمل و دل‌گرمی\nمنبع: گنجور\n{source_url}", "image_url": ""}
    except Exception as exc:
        LOGGER.warning("Daily Hafez fal unavailable (%s)", type(exc).__name__)
        return None
