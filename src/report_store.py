"""Public report snapshots contain content only, never chat IDs or credentials."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from src.news import safe_url

MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_REPORT_AGE = timedelta(hours=36)


def validate_report(report, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(report, dict) or report.get("version") != 1:
        raise ValueError("Unsupported report snapshot version")
    try:
        generated = datetime.fromisoformat(report["generated_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid report timestamp") from exc
    if generated.tzinfo is None or not now - MAX_REPORT_AGE <= generated <= now + timedelta(minutes=5):
        raise ValueError("Report snapshot is stale or has a future timestamp")
    messages = report.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 48:
        raise ValueError("Invalid report message list")
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Invalid report card")
        text = message.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("Invalid report text")
        card = {"text": text, "image_url": safe_url(message.get("image_url", ""))}
        for field in ("document_url", "source_url"):
            if message.get(field):
                card[field] = safe_url(message[field])
        cleaned.append(card)
    return {"version": 1, "generated_at": generated.isoformat(), "messages": cleaned}


def save_report(path, report):
    report = validate_report(report)
    data = json.dumps(report, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Report snapshot exceeds size limit")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def load_report(path):
    with Path(path).open("rb") as file:
        data = file.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Report snapshot exceeds size limit")
    return validate_report(json.loads(data))
