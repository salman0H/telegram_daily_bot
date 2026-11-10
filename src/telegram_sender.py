"""Telegram delivery without unsafe HTML truncation or blind POST retries."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from src import config
from src.formatting import render, split_message, utf16_length
from src.news import safe_url
from src.media import photo_variants

LOGGER = logging.getLogger(__name__)
_TARGET = ContextVar("telegram_delivery_target", default=None)
_LAST_SEND = 0.0
MAX_AUDIO_BYTES = 45 * 1024 * 1024


class TelegramError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class RecipientUnavailableError(TelegramError):
    """Telegram forbids delivery: blocked bot, deactivated user, or no chat access."""
    def __init__(self, message, *, chat_id=None):
        super().__init__(message, status_code=403)
        self.chat_id = str(chat_id) if chat_id is not None else None


@contextmanager
def delivery_target(chat_id):
    """Scope all text, photo, and audio delivery without changing global config."""
    token = _TARGET.set(str(chat_id))
    try:
        yield
    finally:
        _TARGET.reset(token)


def _resolve_target():
    target = _TARGET.get() or config.TELEGRAM_CHANNEL_ID or config.TELEGRAM_USER_ID
    if not target:
        raise TelegramError("Telegram destination is missing")
    return target


def _post(method, body, content_type, timeout=20):
    global _LAST_SEND
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in range(3):
        # Multiple messages to one chat should not burst above one per second.
        if method in {"sendMessage", "sendPhoto", "sendAudio", "sendDocument"}:
            time.sleep(max(0, 1.05 - (time.monotonic() - _LAST_SEND)))
        request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
        try:
            if method in {"sendMessage", "sendPhoto", "sendAudio", "sendDocument"}:
                _LAST_SEND = time.monotonic()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or not result.get("ok"):
                raise TelegramError("Telegram rejected the response")
            return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeError):
                error = {}
            finally:
                exc.close()
            if status == 429 and attempt < 2:
                parameters = error.get("parameters", {}) if isinstance(error, dict) else {}
                delay = parameters.get("retry_after", 2) if isinstance(parameters, dict) else 2
                if isinstance(delay, (int, float)) and 0 <= delay <= 30:
                    time.sleep(delay)
                    continue
            if status == 403 and method in {"sendMessage", "sendPhoto", "sendAudio", "sendDocument"}:
                raise RecipientUnavailableError(
                    f"Telegram {method}: recipient unavailable (HTTP 403)", chat_id=_resolve_target()) from None
            raise TelegramError(f"Telegram {method} failed (HTTP {status})", status_code=status) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # A lost response may already have been delivered. Do not duplicate it.
            raise TelegramError(f"Telegram {method} failed ({type(exc).__name__}); delivery uncertain") from None
    raise TelegramError("Telegram retry limit reached")


def call_api(method, payload=None, timeout=20):
    result = _post(method, json.dumps(payload or {}).encode(), "application/json", timeout=timeout)
    if "result" not in result:
        raise TelegramError(f"Telegram {method} returned no result")
    return result["result"]


def send_message(text, *, reply_markup=None):
    if not text or not text.strip():
        return []
    responses = []
    for index, (chunk, entities) in enumerate(split_message(text)):
        responses.append(_send_text_chunk(chunk, entities, reply_markup if index == 0 else None))
    return responses


def _send_text_chunk(text, entities, reply_markup=None):
    payload = {"chat_id": _resolve_target(), "text": text, "entities": entities,
               "link_preview_options": {"is_disabled": True}}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", json.dumps(payload).encode(), "application/json")


def _send_photo_chunk(photo_url, caption, entities):
    payload = {"chat_id": _resolve_target(), "photo": photo_url,
               "caption": caption, "caption_entities": entities}
    return _post("sendPhoto", json.dumps(payload).encode(), "application/json", timeout=45)


def _send_best_photo(photo_url, caption, entities):
    variants = photo_variants(photo_url)
    for index, candidate in enumerate(variants):
        try:
            return _send_photo_chunk(candidate, caption, entities)
        except RecipientUnavailableError:
            raise
        except TelegramError as exc:
            # Only an explicit rejection is safe to retry with a different URL.
            # A lost response may already have delivered the larger photo.
            if exc.status_code not in {400, 413, 422} or index == len(variants) - 1:
                raise
            LOGGER.info("Larger source image rejected; trying original rendition")
    raise TelegramError("Photo requires a valid HTTP(S) URL")


def send_photo(photo_url, caption=""):
    """Send publisher-hosted artwork by URL, using Telegram's caption entities."""
    photo_url = safe_url(photo_url)
    if not photo_url:
        raise TelegramError("Photo requires a valid HTTP(S) URL")
    caption, entities = render(caption)
    if utf16_length(caption) > 1024:
        raise TelegramError("Photo caption exceeds 1024 UTF-16 units")
    return _send_best_photo(photo_url, caption, entities)


def send_illustrated_message(text, image_url=""):
    """Use a photo card when available, preserving all text on overflow or failure."""
    image_url = safe_url(image_url)
    if not image_url:
        return send_message(text)
    chunks = iter(split_message(text, limit=1024))
    caption, entities = next(chunks, ("", []))
    try:
        photo_result = _send_best_photo(image_url, caption, entities)
    except RecipientUnavailableError:
        raise
    except TelegramError as exc:
        LOGGER.warning("Photo unavailable; sending text instead: %s", exc)
        return send_message(text)
    responses = [photo_result]
    # Keep continuation outside the photo fallback: already-delivered text must
    # never be replayed when only a later continuation fails.
    for chunk, chunk_entities in chunks:
        responses.append(_send_text_chunk(chunk, chunk_entities))
    return responses


def send_audio(file_path, caption=""):
    if not file_path or not os.path.isfile(file_path):
        return None
    if not 0 < os.path.getsize(file_path) <= MAX_AUDIO_BYTES:
        raise TelegramError("Audio must be nonempty and at most 45 MiB")
    caption, entities = render(caption)
    if utf16_length(caption) > 1024:
        raise TelegramError("Audio caption exceeds 1024 UTF-16 units")
    boundary = uuid.uuid4().hex
    body = bytearray()
    fields = {"chat_id": _resolve_target(), "caption": caption, "caption_entities": json.dumps(entities)}
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    # Fixed multipart filename prevents header injection from local paths.
    mime_type = mimetypes.guess_type(file_path)[0] or "audio/mpeg"
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.mp3"\r\nContent-Type: {mime_type}\r\n\r\n'.encode())
    with open(file_path, "rb") as audio:
        body.extend(audio.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return _post("sendAudio", bytes(body), f"multipart/form-data; boundary={boundary}", timeout=90)


def send_original(card):
    url = safe_url(card.get("document_url", ""))
    if not url:
        return
    source = safe_url(card.get("source_url", ""))
    payload = {"chat_id": _resolve_target(), "document": url,
               "caption": "فایل اصلی تصویر منتخب APOD؛ بدون فشرده‌سازی عکس تلگرام"}
    if source:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "منبع ناسا", "url": source}]]}
    try:
        return call_api("sendDocument", payload)
    except RecipientUnavailableError:
        raise
    except TelegramError:
        LOGGER.warning("Original image document unavailable; sending download link")
        return send_message("دریافت فایل اصلی از منبع:\n" + url)
