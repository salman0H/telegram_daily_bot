"""Prefer larger source-CDN renditions without changing the underlying image."""
import re
from urllib.parse import urlsplit, urlunsplit


def photo_variants(url):
    """Return a larger known CDN rendition followed by the original as fallback."""
    if not isinstance(url, str) or not url:
        return []
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return []
    except ValueError:
        return []
    path = parts.path
    if parts.hostname == "ichef.bbci.co.uk":
        match = re.match(r"^/(?:news|ace/standard)/(\d+)(?=/)", path)
        if match and int(match[1]) < 1024:
            path = path[:match.start(1)] + "1024" + path[match.end(1):]
    elif parts.hostname.endswith(".mzstatic.com"):
        match = re.search(r"/(\d+)x(\d+)(bb(?:-\d+)?\.(?:jpg|jpeg|png|webp))$", path, re.IGNORECASE)
        if match and max(int(match[1]), int(match[2])) < 1000:
            path = path[:match.start()] + "/1000x1000" + match[3]
    larger = urlunsplit(parts._replace(path=path))
    return [larger, url] if larger != url else [url]


def image_area(image):
    """Compare provider-supplied image sizes; missing or malformed sizes rank last."""
    if not isinstance(image, dict):
        return 0
    try:
        width = max(0, int(image.get("width") or 0))
        height = max(0, int(image.get("height") or 0))
        return width * height
    except (ValueError, TypeError):
        return 0
