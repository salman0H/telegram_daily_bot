"""NASA APOD selection: original media metadata stays outside model output."""
from datetime import date, datetime, timedelta, timezone
import json
import logging
from urllib.parse import urlencode
from urllib.error import HTTPError
from src import config, writer
from src.http_client import request_json
from src.news import safe_url

LOGGER = logging.getLogger(__name__)


def get_card():
    if not config.ENABLE_NASA:
        return None
    try:
        item = request_json('https://api.nasa.gov/planetary/apod?' + urlencode({'api_key': config.NASA_API_KEY}), timeout=8, attempts=1)
        day = date.fromisoformat(item['date'])
        today = datetime.now(timezone.utc).date()
        if not today - timedelta(days=2) <= day <= today:
            return None
        title = writer.plain_field(item.get('title'), 240)
        explanation = writer.plain_field(item.get('explanation'), 4000)
        if not title or not explanation:
            return None
        source = 'https://apod.nasa.gov/apod/ap' + day.strftime('%y%m%d') + '.html'
        records = writer._call_groq('فقط متن داده‌شدهٔ تصویر منتخب نجومی را به فارسی توضیح بده؛ هیچ جزئیات تازه نساز. فقط JSON با items شامل id برابر nasa و title و summary. عنوان حداکثر ۱۰۰ و توضیح حداکثر ۸۰۰ نویسه. داده‌ها:\n' + json.dumps({'title': title, 'explanation': explanation}, ensure_ascii=False))
        edited = writer._validated_records(records, {'nasa': {}}, {'title': 100, 'summary': 800}).get('nasa')
        body = f"**{edited['title']}**\n{edited['summary']}" if edited else f'**{title}**\n{explanation}\n(متن اصلی؛ ترجمهٔ فارسی در دسترس نیست)'
        text = f'🔭 **منتخب نجومی ناسا · APOD**\nتاریخ انتخاب APOD: {day.isoformat()}\nاین تاریخ، زمان ثبت عکس نیست.\n\n{body}\n\nاعتبار تصویر: {writer.plain_field(item.get("copyright"), 200) or "NASA / APOD؛ جزئیات در منبع"}\n{source}'
        if item.get('media_type') != 'image':
            return {'text': text + '\nامروز رسانهٔ APOD تصویری نیست؛ مشاهده در منبع.', 'image_url': ''}
        image = safe_url(item.get('url'))
        original = safe_url(item.get('hdurl')) or image
        if not image or not original:
            return None
        return {'text': text, 'image_url': image, 'document_url': original, 'source_url': source}
    except HTTPError as exc:
        LOGGER.warning("NASA APOD unavailable (HTTP %s); key mode: %s", exc.code, "demo" if config.NASA_API_KEY == "DEMO_KEY" else "personal")
        exc.close()
        return None
    except Exception as exc:
        LOGGER.warning('NASA APOD unavailable (%s)', type(exc).__name__)
        return None
