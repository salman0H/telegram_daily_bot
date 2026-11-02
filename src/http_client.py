"""Bounded requests with safe logs: never log URLs containing credentials."""
import json
import logging
import time
import urllib.error
import urllib.request

LOGGER = logging.getLogger(__name__)


def request_bytes(url, *, data=None, headers=None, timeout=15, attempts=3):
    headers = {"User-Agent": "TelegramDailyBot/2.0", **(headers or {})}
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise
            try:
                delay = min(15, max(0, float(exc.headers.get("Retry-After", 2 ** attempt))))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            LOGGER.warning("HTTP %s; retry %s/%s", exc.code, attempt + 1, attempts)
            exc.close()
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Request attempts exhausted")


def request_json(url, **kwargs):
    return json.loads(request_bytes(url, **kwargs).decode("utf-8"))
