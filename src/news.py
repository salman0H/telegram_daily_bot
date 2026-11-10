"""Collect dated RSS entries; feed text is untrusted input, not instructions."""
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from src import config
from src.http_client import request_bytes

LOGGER = logging.getLogger(__name__)
FEEDS = {
    "جهان و جامعه": ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    "اقتصاد": ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    "فرهنگ و هنر": ("BBC", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    "علم و فناوری": ("BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
}


def _strip_html(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", text or ""))).strip()


def safe_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return ""
        if any(char.isspace() or ord(char) < 32 for char in value):
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return ""


def _feed_image(entry):
    """Prefer declared image media, then the feed's thumbnails and enclosures."""
    namespace = "http://search.yahoo.com/mrss/"
    content = [element for element in entry.findall(f".//{{{namespace}}}content")
               if element.get("medium", "").lower() == "image"
               or element.get("type", "").lower().startswith("image/")]
    thumbnails = entry.findall(f".//{{{namespace}}}thumbnail")
    enclosures = [element for element in entry.findall("enclosure")
                  if element.get("type", "").lower().startswith("image/")]
    for group in (content, thumbnails, enclosures):
        def size(element):
            try:
                return int(element.get("width", "0")) * int(element.get("height", "0"))
            except ValueError:
                return 0
        for element in sorted(group, key=size, reverse=True):
            image = safe_url(element.get("url", ""))
            if image:
                return image
    return ""


def interest_score(text):
    text = text.casefold()
    return sum(bool(re.search(r"(?<!\w)" + re.escape(term.strip().casefold()) + r"(?!\w)", text)) for term in config.NEWS_KEYWORDS if term.strip())


def get_diverse_news(now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config.NEWS_MAX_AGE_HOURS)
    news_data, seen_links, seen_titles = {}, set(), set()
    for category, (source, url) in FEEDS.items():
        try:
            root = ET.fromstring(request_bytes(url))
            candidates = []
            for entry in root.findall(".//item"):
                title = _strip_html(entry.findtext("title"))[:240]
                link = safe_url(entry.findtext("link") or "")
                try:
                    published = parsedate_to_datetime(entry.findtext("pubDate") or "")
                    if published.tzinfo is None:
                        continue
                except (ValueError, TypeError, OverflowError):
                    continue
                if not title or not link or not cutoff <= published <= now + timedelta(minutes=5):
                    continue
                candidates.append((published, title, link, entry))
            items = []
            for published, title, link, entry in sorted(candidates, key=lambda item: (interest_score(item[1] + " " + _strip_html(item[3].findtext("description"))), item[0]), reverse=True):
                title_key = title.casefold()
                if link in seen_links or title_key in seen_titles:
                    continue
                seen_links.add(link)
                seen_titles.add(title_key)
                items.append({"title": title, "summary": _strip_html(entry.findtext("description"))[:1200],
                              "link": link, "source": source, "published_at": published.isoformat(),
                              "image_url": _feed_image(entry)})
                if len(items) == config.NEWS_PER_CATEGORY:
                    break
            if items:
                news_data[category] = items
        except Exception as exc:
            LOGGER.warning("News feed %s unavailable (%s)", category, type(exc).__name__)
    return news_data
