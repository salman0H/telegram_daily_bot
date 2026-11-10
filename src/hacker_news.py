"""A bounded selection of recent submissions from the official Hacker News API."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging

from src import config, writer
from src.http_client import request_json
from src.news import _strip_html, safe_url

LOGGER = logging.getLogger(__name__)
API = "https://hacker-news.firebaseio.com/v0"
MAX_CANDIDATES = 25


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _fetch_story(item_id, now):
    try:
        item = request_json(f"{API}/item/{item_id}.json", timeout=5, attempts=1)
        if (not isinstance(item, dict) or not _integer(item.get("id")) or item.get("id") != item_id
                or item.get("type") != "story" or item.get("dead") or item.get("deleted")):
            return None
        timestamp = item.get("time")
        if not _integer(timestamp):
            return None
        submitted = datetime.fromtimestamp(timestamp, timezone.utc)
        if not now - timedelta(hours=config.NEWS_MAX_AGE_HOURS) <= submitted <= now + timedelta(minutes=5):
            return None
        title = _strip_html(item.get("title"))[:240] if isinstance(item.get("title"), str) else ""
        if not title:
            return None
        discussion = f"https://news.ycombinator.com/item?id={item_id}"
        raw_url = item.get("url")
        link = discussion if raw_url is None or raw_url == "" else safe_url(raw_url)
        if not link:
            return None
        text = _strip_html(item.get("text"))[:1200] if isinstance(item.get("text"), str) else ""
        return {"title": title, "summary": text, "link": link, "source": "Hacker News",
                "published_at": submitted.isoformat(), "discussion_url": discussion,
                "score": item.get("score") if _integer(item.get("score")) else None,
                "comments": item.get("descendants") if _integer(item.get("descendants")) else None,
                "image_url": ""}
    except Exception as exc:
        LOGGER.info("Hacker News item unavailable (%s)", type(exc).__name__)
        return None


def get_stories(now=None):
    if not config.ENABLE_HACKER_NEWS:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        ranking = request_json(f"{API}/topstories.json", timeout=5, attempts=1)
        if not isinstance(ranking, list):
            raise ValueError("Invalid Hacker News ranking")
        ids = []
        for item_id in ranking[:500]:
            if _integer(item_id) and item_id > 0 and item_id not in ids:
                ids.append(item_id)
                if len(ids) == MAX_CANDIDATES:
                    break
        stories, seen_links, seen_titles = [], set(), set()
        with ThreadPoolExecutor(max_workers=5) as pool:
            for start in range(0, len(ids), 5):
                # map retains the API's ranking, irrespective of response order.
                for item in pool.map(lambda item_id: _fetch_story(item_id, now), ids[start:start + 5]):
                    if item is None or item["link"] in seen_links or item["title"].casefold() in seen_titles:
                        continue
                    stories.append(item)
                    seen_links.add(item["link"])
                    seen_titles.add(item["title"].casefold())
                    if len(stories) >= config.HACKER_NEWS_PER_DAY:
                        return stories
        return stories
    except Exception as exc:
        LOGGER.warning("Hacker News unavailable (%s)", type(exc).__name__)
        return []


def prepare_cards(stories):
    cards = writer.prepare_news_cards({"فناوری و جامعهٔ Hacker News": stories})
    for card, story in zip(cards, stories):
        stats = []
        if story["score"] is not None:
            stats.append(f"امتیاز: {story['score']}")
        if story["comments"] is not None:
            stats.append(f"نظرها: {story['comments']}")
        basis = "بر پایهٔ عنوان و متن پست HN" if story["summary"] else "بر پایهٔ عنوان ثبت‌شده در HN؛ متن مقاله دریافت نشده"
        card["text"] += f"\n\n{basis}\nزمان بالا، زمان ثبت پست در HN است؛ نه لزوماً انتشار مقاله."
        if stats:
            card["text"] += "\n" + " · ".join(stats) + " (هنگام دریافت)"
        if story["discussion_url"] != story["link"]:
            card["text"] += f"\nگفت‌وگو در Hacker News:\n{story['discussion_url']}"
    return cards
