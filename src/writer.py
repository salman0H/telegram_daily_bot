"""Generate validated editorial fields; source metadata stays outside the LLM."""
import json
import logging
import re
from datetime import datetime
from src import config
from src.http_client import request_json
from src.insights import INSIGHTS
from src.news import safe_url
from src.persian import normalize_prose
from src.prompts import SYSTEM_PROMPT, NEWS_PROMPT, INSIGHT_PROMPT

LOGGER = logging.getLogger(__name__)


def plain_field(value, limit=500):
    if not isinstance(value, str):
        return ""
    value = normalize_prose(re.sub(r"<[^>]*>", "", value))
    value = re.sub(r"https?://\S+", "", value)
    return re.sub(r"\s+", " ", value.replace("*", "").replace("`", "").replace("\u200b", "")).strip()[:limit]


def _call_groq(prompt, temperature=0.15):
    if not config.GROQ_API_KEY:
        return None
    for model in config.GROQ_MODELS:
        payload = {"model": model, "messages": [
            {"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            "temperature": temperature, "max_completion_tokens": 4096,
            "response_format": {"type": "json_object"}}
        try:
            result = request_json(config.GROQ_API_URL, data=json.dumps(payload).encode(),
                                  headers={"Authorization": f"Bearer {config.GROQ_API_KEY}",
                                           "Content-Type": "application/json"}, timeout=45, attempts=2)
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete completion")
            content = json.loads(choice["message"]["content"])
            if not isinstance(content, dict) or not isinstance(content.get("items"), list):
                raise ValueError("Invalid editorial schema")
            return content["items"]
        except Exception as exc:
            LOGGER.warning("Editorial model %s unavailable or invalid (%s)", model, type(exc).__name__)
    return None


def _validated_records(records, originals, fields):
    """Reject unknown IDs, duplicate IDs, empty fields and oversized fields."""
    accepted, duplicates = {}, set()
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = record.get("id")
        if not isinstance(key, str) or key not in originals:
            continue
        if key in accepted or key in duplicates:
            accepted.pop(key, None)
            duplicates.add(key)
            continue
        if all(isinstance(record.get(field), str) and 0 < len(record[field].strip()) <= limit
               and plain_field(record[field], limit) for field, limit in fields.items()):
            accepted[key] = {field: plain_field(record[field], limit) for field, limit in fields.items()}
    return accepted


def _source_line(item):
    stamp = ""
    if item.get("published_at"):
        try:
            stamp = " | " + datetime.fromisoformat(item["published_at"]).astimezone(config.LOCAL_TZ).strftime("%Y/%m/%d، %H:%M")
        except ValueError:
            pass
    return f"منبع: {plain_field(item.get('source', ''), 80)}{stamp}\n{item['link']}"


def prepare_news_cards(news_data):
    """Return one card per story, retaining its source-owned image association."""
    originals, categories = {}, {}
    for category, items in news_data.items():
        for item in items:
            key = f"n{len(originals) + 1}"
            originals[key] = item
            categories.setdefault(category, []).append(key)
    if not originals:
        return []
    # Only source text enters the model; URLs and categories are rendered locally.
    data = [{"id": key, "title": item["title"], "summary": item.get("summary", "")}
            for key, item in originals.items()]
    records = _call_groq(NEWS_PROMPT + json.dumps(data, ensure_ascii=False))
    edited = _validated_records(records, originals, {"title": 100, "summary": 360})
    cards = []
    for category, keys in categories.items():
        for key in keys:
            original = originals[key]
            item = edited.get(key)
            if item:
                body = f"▪️ **{item['title']}**\n{item['summary']}"
            else:
                body = (f"▪️ **{plain_field(original['title'], 240)}**\n"
                        f"{plain_field(original.get('summary', ''), 500)}\n"
                        "(متن اصلی منبع؛ بازنویسی فارسی در دسترس نیست)")
            cards.append({"text": f"📰 **{plain_field(category)}**\n\n{body}\n{_source_line(original)}",
                          "image_url": safe_url(original.get("image_url", ""))})
    return cards


def elaborate_news(news_data):
    """Text-only view retained for integrations that consume the full digest."""
    cards = prepare_news_cards(news_data)
    if not cards:
        return "خبر تازهٔ دارای تاریخ معتبر در بازهٔ تعیین‌شده دریافت نشد."
    return "خلاصهٔ خبرها بر پایهٔ خوراک منبع؛ بدون تأیید مستقل.\n\n" + "\n\n".join(
        card["text"] for card in cards)


def generate_daily_insight(now=None):
    day = (now or datetime.now(config.LOCAL_TZ)).date().toordinal()
    selected = [INSIGHTS[(day * 2 + index) % len(INSIGHTS)] for index in range(2)]
    originals = {f"k{index + 1}": item for index, item in enumerate(selected)}
    data = [{"id": key, **{field: item[field] for field in ("title", "summary", "action")}}
            for key, item in originals.items()]
    records = _call_groq(INSIGHT_PROMPT + json.dumps(data, ensure_ascii=False))
    edited = _validated_records(records, originals, {"title": 80, "summary": 300, "action": 180})
    blocks = []
    for key, original in originals.items():
        item = edited.get(key, original)
        blocks.append(f"🔹 **{item['title']}**\n{item['summary']}\n\n"
                      f"تمرین امروز: {item['action']}\n{_source_line(original)}")
    return "\n\n".join(blocks)


def explain_hafez(title, verse, source_summary=''):
    """Prefer the source's attributed reading; use grounded model prose only as fallback."""
    summary = plain_field(source_summary, 2400)
    summary = re.sub(r'^هوش\s*مصنوعی\s*[:：]\s*', '', summary).strip()
    if len(summary) >= 40 and len(re.findall(r'[\u0600-\u06FF]', summary)) >= 10:
        if len(summary) > 900:
            # Prefer complete sentences, otherwise stop at a word boundary.
            end = max(summary.rfind('。', 0, 900), summary.rfind('.', 0, 900), summary.rfind('؟', 0, 900))
            summary = summary[:end + 1] if end >= 400 else summary[:900].rsplit(' ', 1)[0] + '…'
        return summary, 'برداشت ادبی گنجور · تولید هوش مصنوعی'
    from src.prompts import HAFEZ_EXPLANATION_PROMPT
    records = _call_groq(HAFEZ_EXPLANATION_PROMPT + json.dumps({'title': title, 'poem': verse}, ensure_ascii=False))
    accepted = _validated_records(records, {'fal': {}}, {'summary': 800})
    if 'fal' in accepted and len(re.findall(r'[\u0600-\u06FF]', accepted['fal']['summary'])) >= 10:
        return accepted['fal']['summary'], 'برداشت ادبی · تولید هوش مصنوعی'
    return 'توضیح این غزل فعلاً در دسترس نیست؛ می‌توانید غزل را همراه با منبع اصلی بخوانید.', 'توضیح غزل'
