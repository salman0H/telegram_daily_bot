"""Authenticated, encrypted subscriber state; never part of the public report."""
import base64
from datetime import date
import hashlib
import hmac
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from src import config

MAX_USERS = 10000
MAX_BYTES = 2 * 1024 * 1024


def _cipher():
    key = config.SUBSCRIBER_STATE_KEY
    if not key:
        if not config.TELEGRAM_BOT_TOKEN:
            raise ValueError("Telegram token or SUBSCRIBER_STATE_KEY is required for subscriber encryption")
        # Bot tokens are high-entropy secrets, not human passwords. Domain separation
        # keeps this encryption key distinct from the Telegram API credential.
        key = base64.urlsafe_b64encode(hmac.new(
            config.TELEGRAM_BOT_TOKEN.encode(), b"daily-bot/subscribers/v1", hashlib.sha256).digest())
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValueError("SUBSCRIBER_STATE_KEY must be a Fernet key") from None


class Subscribers:
    def __init__(self, users=None):
        self.users = users if users is not None else {}
        self._saved_raw = None

    def subscribe(self, user_id):
        key = str(user_id)
        if key not in self.users:
            if len(self.users) >= MAX_USERS:
                raise ValueError("Subscriber capacity reached")
            self.users[key] = {"last_daily": None}

    def remove(self, user_id):
        self.users.pop(str(user_id), None)

    def delivered(self, user_id, day):
        if str(user_id) in self.users:
            record = self.users[str(user_id)]
            if record["last_daily"] is None or day > record["last_daily"]:
                record["last_daily"] = day

    def save(self, path):
        raw = json.dumps({"version": 1, "users": self.users}, separators=(",", ":")).encode()
        if raw == self._saved_raw:
            return
        encrypted = _cipher().encrypt(raw)
        if len(encrypted) > MAX_BYTES:
            raise ValueError("Subscriber state exceeds size limit")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(encrypted)
        temporary.chmod(0o600)
        temporary.replace(path)
        self._saved_raw = raw

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open("rb") as stream:
            encrypted = stream.read(MAX_BYTES + 1)
        if len(encrypted) > MAX_BYTES:
            raise ValueError("Subscriber state exceeds size limit")
        try:
            data = json.loads(_cipher().decrypt(encrypted))
        except (InvalidToken, ValueError, UnicodeError):
            raise ValueError("Cannot decrypt subscriber state; keep the existing encryption key and artifact") from None
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Invalid subscriber state version")
        users = data.get("users")
        if not isinstance(users, dict) or len(users) > MAX_USERS:
            raise ValueError("Invalid subscriber list")
        for key, value in users.items():
            if not key.isascii() or not key.isdigit() or not 0 < int(key) < 2**52 or str(int(key)) != key:
                raise ValueError("Invalid subscriber identifier")
            if not isinstance(value, dict) or set(value) != {"last_daily"}:
                raise ValueError("Invalid subscriber record")
            day = value["last_daily"]
            if day is not None:
                if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                    raise ValueError("Invalid subscriber delivery date")
        state = cls(users)
        state._saved_raw = json.dumps({"version": 1, "users": users}, separators=(",", ":")).encode()
        return state
