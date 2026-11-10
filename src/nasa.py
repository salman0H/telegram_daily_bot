"""NASA APOD selection: original media metadata stays outside model output."""
from datetime import date, datetime, timedelta, timezone
import json
import html
import re
import logging
from urllib.parse import urlencode
from urllib.error import HTTPError
from src import config, writer
from src.http_client import request_json, request_bytes
from src.news import safe_url

LOGGER = logging.getLogger(__name__)


def _valid_item(item):
    if not isinstance(item, dict):
        raise ValueError("Invalid APOD metadata")
    day = date.fromisoformat(item['date'])
    today = datetime.now(timezone.utc).date()
    if not today - timedelta(days=2) <= day <= today:
        raise ValueError("Stale APOD selection")
    if not item.get('title') or not item.get('explanation'):
        raise ValueError("Incomplete APOD metadata")
    if item.get('media_type') == 'image' and not safe_url(item.get('url')):
        raise ValueError("Missing APOD image")
    return item


def _page_item():
    landing = request_bytes('https://science.nasa.gov/apod/', timeout=10, attempts=2).decode('utf-8')
    links = re.findall(r'href=["\'](https://science\.nasa\.gov/image-article/apod-\d{4}-[a-z]+-\d{1,2}-[^"\']+/)["\']', landing)
    if not links:
        raise ValueError("No current APOD article link")
    source = html.unescape(links[0])
    page = request_bytes(source, timeout=10, attempts=2).decode('utf-8')
    title_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
    explanation = re.search(r'<strong>Explanation:</strong>(.*?)(?:<br\s*/?>\s*<br\s*/?>|</p>)', page, re.S)
    original = re.search(r'<a[^>]+href="(https://assets\.science\.nasa\.gov/content/dam/[^"?]+\.(?:jpg|jpeg|png|gif))"', page, re.I)
    credit = re.search(r'Credit(?: &amp; Copyright)?</th>\s*<td[^>]*>(.*?)</td>', page, re.S)
    if not title_match or not explanation or not original:
        raise ValueError("Incomplete APOD article")
    title = html.unescape(title_match[1])
    match = re.match(r'APOD: (\d{4} [A-Za-z]+ \d{1,2}) [–-] (.*?)(?: - NASA Science)?$', title)
    if not match:
        raise ValueError("Unrecognized APOD date")
    day = datetime.strptime(match[1], '%Y %B %d').date()
    clean = lambda value: html.unescape(re.sub(r'<[^>]*>', ' ', value)).strip()
    item = {'date': day.isoformat(), 'title': match[2], 'explanation': clean(explanation[1]),
            'media_type': 'image', 'url': html.unescape(original[1]), 'hdurl': html.unescape(original[1]),
            'copyright': clean(credit[1]) if credit else '', 'source_url': source}
    _valid_item(item)
    LOGGER.info("NASA APOD fallback selected: %s; original image found", item['date'])
    return item


def _load_item():
    try:
        return _valid_item(request_json('https://api.nasa.gov/planetary/apod?' + urlencode({'api_key': config.NASA_API_KEY}), timeout=8, attempts=1))
    except Exception as exc:
        status = exc.code if isinstance(exc, HTTPError) else type(exc).__name__
        LOGGER.warning("NASA API failed (%s); trying official APOD page", status)
        if isinstance(exc, HTTPError):
            exc.close()
        return _page_item()


def get_card():
    if not config.ENABLE_NASA:
        return None
    try:
        item = _load_item()
        day = date.fromisoformat(item['date'])
        today = datetime.now(timezone.utc).date()
        if not today - timedelta(days=2) <= day <= today:
            return None
        title = writer.plain_field(item.get('title'), 240)
        explanation = writer.plain_field(item.get('explanation'), 4000)
        if not title or not explanation:
            return None
        source = item.get('source_url') or 'https://apod.nasa.gov/apod/ap' + day.strftime('%y%m%d') + '.html'
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
