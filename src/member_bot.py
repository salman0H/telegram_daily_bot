"""Bounded Telegram request processing for scheduled, serverless Actions runs."""
import argparse
from datetime import datetime
import logging
import re
import time
from urllib.parse import urlsplit
from src import config, telegram_sender as telegram
from src.news import safe_url
from src.report_store import load_report, validate_report
from src.subscribers import Subscribers

LOGGER = logging.getLogger(__name__)
ALLOWED_UPDATES = ["message", "callback_query"]
REPORT_BUTTON = {"inline_keyboard": [[{"text": "📰 دریافت گزارش", "callback_data": "report"}]]}


def join_url():
    if config.CHANNEL_JOIN_URL:
        url = safe_url(config.CHANNEL_JOIN_URL)
        if not url or urlsplit(url).scheme != "https" or urlsplit(url).hostname not in {"t.me", "telegram.me"}:
            raise ValueError("CHANNEL_JOIN_URL must be an HTTPS Telegram channel link")
        return url
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", config.MEMBERSHIP_CHANNEL_ID):
        return "https://t.me/" + config.MEMBERSHIP_CHANNEL_ID[1:]
    raise ValueError("Set CHANNEL_JOIN_URL for a private membership channel")


def is_member(member):
    if not isinstance(member, dict):
        return False
    return member.get("status") in {"creator", "administrator", "member"} or (
        member.get("status") == "restricted" and member.get("is_member") is True)


class MemberBot:
    def __init__(self, report=None, *, clock=time.monotonic, max_reports=10, runtime_seconds=240,
                 subscribers=None, checkpoint=None):
        self.report = validate_report(report) if report is not None else None
        self.clock = clock
        self.deadline = clock() + runtime_seconds
        self.max_reports = max_reports
        self.report_count = 0
        self.membership_checks = 0
        self.updates_processed = 0
        self.served = set()
        self.unreachable = set()
        self.username = ""
        self.channel_link = join_url()
        self.subscribers = subscribers
        self.checkpoint = checkpoint or (lambda: None)
        self.reports_completed = 0

    def check_setup(self):
        if not config.TELEGRAM_BOT_TOKEN or not config.MEMBERSHIP_CHANNEL_ID:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and MEMBERSHIP_CHANNEL_ID")
        webhook = telegram.call_api("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise ValueError("Polling requires an empty webhook; an existing webhook is never removed automatically")
        identity = telegram.call_api("getMe")
        if not isinstance(identity, dict) or not isinstance(identity.get("id"), int):
            raise ValueError("Could not identify the Telegram bot")
        self.username = identity.get("username", "").casefold()
        LOGGER.info("Responding as @%s; membership channel: %s", self.username, config.MEMBERSHIP_CHANNEL_ID)
        membership = telegram.call_api("getChatMember", {"chat_id": config.MEMBERSHIP_CHANNEL_ID, "user_id": identity["id"]})
        if not isinstance(membership, dict) or membership.get("status") not in {"administrator", "creator"}:
            raise ValueError("The bot must be an administrator in MEMBERSHIP_CHANNEL_ID")

    def _send(self, user_id, text, keyboard=None):
        with telegram.delivery_target(user_id):
            telegram.send_message(text, reply_markup=keyboard)

    def _require_membership(self, user_id, *, subscribe=False):
        self.membership_checks += 1
        try:
            member = telegram.call_api("getChatMember", {"chat_id": config.MEMBERSHIP_CHANNEL_ID, "user_id": user_id})
        except telegram.TelegramError:
            self._send(user_id, "بررسی عضویت فعلاً ممکن نیست. لطفاً کمی بعد دوباره /report را بفرستید.")
            LOGGER.warning("Membership check unavailable; report access denied")
            return False
        if is_member(member):
            return True
        keyboard = {"inline_keyboard": [
            [{"text": "📣 عضویت در کانال", "url": self.channel_link}],
            [{"text": "✅ عضو شدم؛ بررسی مجدد", "callback_data":
              "subscribe_after_join" if subscribe and self.subscribers is not None else "check_membership"}],
        ]}
        self._send(user_id, "برای دریافت گزارش، ابتدا عضو کانال شوید. بعد از عضویت، «بررسی مجدد» را بزنید.", keyboard)
        return False

    def _report_day(self):
        if not self.report:
            return None
        return datetime.fromisoformat(self.report["generated_at"]).astimezone(config.LOCAL_TZ).date().isoformat()

    def _deliver_cards(self, user_id):
        with telegram.delivery_target(user_id):
            for card in self.report["messages"]:
                if self.clock() >= self.deadline - 10:
                    self._send(user_id, "ارسال این گزارش کامل نشد. لطفاً چند دقیقه بعد دوباره /report را بفرستید.", REPORT_BUTTON)
                    return False
                telegram.send_illustrated_message(card["text"], card["image_url"])
                telegram.send_original(card)
        self.reports_completed += 1
        if self.subscribers is not None:
            self.subscribers.delivered(user_id, self._report_day())
            self.checkpoint()
        return True

    def _request_report(self, user_id, *, subscribe=False):
        # Membership is always checked live, including button clicks and repeat requests.
        if not self._require_membership(user_id, subscribe=subscribe):
            return
        if subscribe and self.subscribers is not None:
            self.subscribers.subscribe(user_id)
            self.checkpoint()
            self._send(user_id, "🔔 گزارش روزانه برای شما فعال شد. برای لغو ارسال خودکار /stop را بفرستید.\n"
                       "پاسخ‌گویی و ارسال به زمان اجرای GitHub وابسته است و ممکن است با تأخیر انجام شود.")
        if user_id in self.served:
            return
        if not self.report:
            self._send(user_id, "گزارش تازه هنوز آماده نیست. لطفاً بعد از آماده شدن گزارش روزانه دوباره /report را بفرستید.", REPORT_BUTTON)
            return
        self.served.add(user_id)
        self.report_count += 1
        stamp = datetime.fromisoformat(self.report["generated_at"]).astimezone(config.LOCAL_TZ).strftime("%Y/%m/%d، %H:%M")
        self._send(user_id, f"✅ عضویت شما تأیید شد. آخرین گزارش آماده‌شده:\n{stamp} ({config.TIMEZONE})")
        self._deliver_cards(user_id)

    def broadcast(self, *, now=None):
        """Deliver today's report to subscribed members, once per local day."""
        now = (now or datetime.now(config.LOCAL_TZ)).astimezone(config.LOCAL_TZ)
        today = now.date().isoformat()
        if (not config.BOT_ENABLED or not config.DAILY_SUBSCRIPTIONS_ENABLED or self.subscribers is None or not self.report
                or self._report_day() != today or now.hour < 7):
            return
        for user_id, record in list(self.subscribers.users.items()):
            if self.report_count >= self.max_reports or self.clock() >= self.deadline - 30:
                break
            if record["last_daily"] == today or int(user_id) in self.served:
                continue
            try:
                # Automatic checks do not send repeated join/error prompts every run.
                self.membership_checks += 1
                member = telegram.call_api("getChatMember", {
                    "chat_id": config.MEMBERSHIP_CHANNEL_ID, "user_id": int(user_id)})
                if not is_member(member):
                    continue
                self.report_count += 1
                self._send(user_id, "☀️ گزارش روزانهٔ شما\nبرای لغو ارسال خودکار: /stop")
                self._deliver_cards(user_id)
            except telegram.RecipientUnavailableError:
                self.subscribers.remove(user_id)
                LOGGER.info("Unavailable subscriber removed; continuing with other recipients")
            except telegram.TelegramError as exc:
                LOGGER.warning("Subscriber delivery/check failed: %s", exc)
            finally:
                self.checkpoint()

    def handle_update(self, update):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        sender = callback.get("from") if isinstance(callback, dict) else (message.get("from") if isinstance(message, dict) else None)
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return
        chat = message.get("chat", {})
        if not isinstance(chat, dict):
            return
        user_id = sender.get("id")
        if (chat.get("type") != "private" or not isinstance(user_id, int) or isinstance(user_id, bool)
                or user_id <= 0 or chat.get("id") != user_id or sender.get("is_bot")):
            return
        if str(user_id) in self.unreachable:
            return
        if isinstance(callback, dict):
            if callback.get("data") not in {"report", "check_membership", "subscribe_after_join"}:
                return
            try:
                telegram.call_api("answerCallbackQuery", {"callback_query_id": callback["id"]})
            except (KeyError, telegram.TelegramError):
                # A delayed Actions run may find the callback acknowledgement expired.
                LOGGER.info("Callback acknowledgement expired or unavailable")
            self._request_report(user_id, subscribe=callback.get("data") == "subscribe_after_join")
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            return
        token = text.split(maxsplit=1)[0] if text.strip() else ""
        command, _, addressed_bot = token.partition("@")
        if addressed_bot and addressed_bot.casefold() != self.username:
            return
        if command in {"/stop", "/unsubscribe"}:
            if self.subscribers is not None:
                self.subscribers.remove(user_id)
                self.checkpoint()
            self._send(user_id, "🔕 ارسال خودکار روزانه غیرفعال شد. برای فعال‌سازی دوباره /start و برای دریافت یک گزارش /report را بفرستید.")
        elif command in {"/start", "/subscribe", "/report", "/run"}:
            self._request_report(user_id, subscribe=command in {"/start", "/subscribe"})
        else:
            self._send(user_id, "📰 گزارش فعلی: /report\n🔔 فعال‌سازی گزارش روزانه: /start\n🔕 لغو ارسال روزانه: /stop\n"
                       "دریافت گزارش نیازمند عضویت در کانال است. پاسخ ممکن است تا اجرای بعدی GitHub تأخیر داشته باشد.", REPORT_BUTTON)

    def process_pending(self, *, listen_seconds=0, check_setup=True):
        if not config.BOT_ENABLED:
            LOGGER.info("Bot is paused (BOT_ENABLED=false)")
            return
        if not 0 <= listen_seconds <= 240:
            raise ValueError("listen_seconds must be between 0 and 240")
        if check_setup:
            self.check_setup()
        listening = listen_seconds > 0
        stop_at = min(self.deadline, self.clock() + listen_seconds) if listening else self.deadline
        if listening:
            self.deadline = stop_at
        offset = None
        LOGGER.info("Processing requests; listen window: %s seconds", listen_seconds)
        while True:
            remaining = stop_at - self.clock()
            timeout = min(20, max(0, int(remaining - 30))) if listening else 0
            payload = {"limit": 100, "timeout": timeout, "allowed_updates": ALLOWED_UPDATES}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram.call_api("getUpdates", payload, timeout=timeout + 20)
            if not isinstance(updates, list):
                raise telegram.TelegramError("Invalid getUpdates result")
            for update in updates:
                if self.report_count >= self.max_reports or self.clock() >= stop_at - 30:
                    break
                if (not isinstance(update, dict) or not isinstance(update.get("update_id"), int)
                        or isinstance(update["update_id"], bool)):
                    raise telegram.TelegramError("Invalid Telegram update identifier")
                try:
                    self.handle_update(update)
                except telegram.RecipientUnavailableError as exc:
                    if exc.chat_id is not None:
                        self.unreachable.add(exc.chat_id)
                        if self.subscribers is not None:
                            self.subscribers.remove(exc.chat_id)
                    LOGGER.info("Recipient unavailable; request skipped")
                except telegram.TelegramError as exc:
                    LOGGER.warning("Request delivery failed: %s", exc)
                self.checkpoint()
                offset = update["update_id"] + 1
                self.updates_processed += 1
                # Confirm only the processed prefix; further updates stay queued.
                telegram.call_api("getUpdates", {"limit": 1, "timeout": 0,
                                                  "allowed_updates": ALLOWED_UPDATES, "offset": offset})
            if not listening or self.report_count >= self.max_reports or self.clock() >= stop_at - 30:
                break
            if not updates:
                # Defensive pacing if Telegram returns an empty long poll early.
                time.sleep(0.25)
        LOGGER.info("Requests complete: %s updates, %s membership checks, %s reports attempted, %s completed",
                    self.updates_processed, self.membership_checks, self.report_count, self.reports_completed)


def main():
    parser = argparse.ArgumentParser(description="Process member-only report requests from Telegram")
    parser.add_argument("--report", default="report-cache/daily-report.json", help="Path to the latest public report snapshot")
    parser.add_argument("--listen-seconds", type=int, default=0,
                        help="Keep receiving new requests for up to 240 seconds; default processes one batch")
    parser.add_argument("--state", help="Encrypted subscriber state to restore; missing file starts an empty list")
    parser.add_argument("--save-state", help="Encrypted output state for the next worker (requires --state)")
    parser.add_argument("--broadcast", action="store_true", help="Also deliver today's report to subscribers")
    parser.add_argument("--broadcast-only", action="store_true", help="Broadcast without consuming Telegram updates")
    parser.add_argument("--destination-already-served", action="store_true",
                        help="Mark the configured private daily destination as delivered before broadcast")
    args = parser.parse_args()
    if not 0 <= args.listen_seconds <= 240:
        parser.error("--listen-seconds must be between 0 and 240")
    if (args.save_state or args.broadcast or args.broadcast_only) and not args.state:
        parser.error("Subscriber delivery requires --state")
    if args.state and not args.save_state:
        parser.error("--state requires --save-state so subscription changes are retained")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not config.BOT_ENABLED:
        LOGGER.info("Bot is paused (BOT_ENABLED=false)")
        return 0
    try:
        subscribers = Subscribers.load(args.state) if args.state else None
        checkpoint = (lambda: subscribers.save(args.save_state)) if subscribers is not None else None
        try:
            report = load_report(args.report)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Report snapshot unavailable (%s)", type(exc).__name__)
            report = None
        bot = MemberBot(report, subscribers=subscribers, checkpoint=checkpoint)
        bot.check_setup()
        try:
            if args.destination_already_served and subscribers is not None and report:
                target = config.TELEGRAM_CHANNEL_ID or config.TELEGRAM_USER_ID
                subscribers.delivered(target, bot._report_day())
                checkpoint()
            if not args.broadcast_only:
                # Process /stop before automatic delivery. New requests remain bounded.
                bot.process_pending(check_setup=False)
            if args.broadcast or args.broadcast_only:
                bot.broadcast()
            if args.listen_seconds and not args.broadcast_only:
                bot.process_pending(listen_seconds=args.listen_seconds, check_setup=False)
        finally:
            if checkpoint:
                checkpoint()
        LOGGER.info("Delivery finished: %s reports completed; %s subscriptions",
                    bot.reports_completed, len(subscribers.users) if subscribers is not None else 0)
    except (ValueError, telegram.TelegramError) as exc:
        LOGGER.error("Member bot configuration/API error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
