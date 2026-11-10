"""Encrypted persistence, opt-out, daily deduplication, and recipient isolation."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from src import config, member_bot, subscribers, telegram_sender as telegram


def request(text, user=42):
    return {"update_id": user, "message": {"chat": {"id": user, "type": "private"},
            "from": {"id": user}, "text": text}}


@patch.object(config, "TELEGRAM_BOT_TOKEN", "test-token-not-a-real-credential")
@patch.object(config, "SUBSCRIBER_STATE_KEY", "")
class EncryptionTests(unittest.TestCase):
    def test_round_trip_contains_no_plaintext_and_keeps_delivery_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subscribers.enc"
            state = subscribers.Subscribers()
            state.subscribe(123456789)
            state.delivered(123456789, "2026-11-10")
            state.save(path)
            self.assertNotIn(b'123456789', path.read_bytes())
            self.assertNotIn(b'last_daily', path.read_bytes())
            self.assertEqual(subscribers.Subscribers.load(path).users, state.users)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_wrong_key_or_corruption_fails_without_resetting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subscribers.enc"
            subscribers.Subscribers().save(path)
            original = path.read_bytes()
            with patch.object(config, "TELEGRAM_BOT_TOKEN", "different-token"):
                with self.assertRaisesRegex(ValueError, "Cannot decrypt"):
                    subscribers.Subscribers.load(path)
            self.assertEqual(path.read_bytes(), original)
            path.write_bytes(b"not-encrypted-json")
            with self.assertRaises(ValueError):
                subscribers.Subscribers.load(path)

    def test_explicit_key_survives_token_rotation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                config, "SUBSCRIBER_STATE_KEY", Fernet.generate_key()):
            path = Path(directory) / "subscribers.enc"
            state = subscribers.Subscribers()
            state.subscribe(42)
            state.save(path)
            with patch.object(config, "TELEGRAM_BOT_TOKEN", "rotated"):
                self.assertIn("42", subscribers.Subscribers.load(path).users)

    def test_unchanged_state_does_not_create_another_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "in.enc", Path(directory) / "out.enc"
            subscribers.Subscribers().save(source)
            state = subscribers.Subscribers.load(source)
            state.save(output)
            self.assertFalse(output.exists())
            state.subscribe(42)
            state.save(output)
            self.assertTrue(output.exists())

    def test_authenticated_but_malformed_records_rejected(self):
        for users in ({"0": {"last_daily": None}}, {"42": {"last_daily": "wrong"}},
                      {"42": {"last_daily": None, "token": "bad"}}, {"42": []}):
            with self.subTest(users=users), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.enc"
                path.write_bytes(subscribers._cipher().encrypt(json.dumps({"version": 1, "users": users}).encode()))
                with self.assertRaises(ValueError):
                    subscribers.Subscribers.load(path)

    def test_older_manual_report_cannot_undo_daily_delivery_marker(self):
        state = subscribers.Subscribers()
        state.subscribe(42)
        state.delivered(42, "2026-11-10")
        state.delivered(42, "2026-11-09")
        self.assertEqual(state.users["42"]["last_daily"], "2026-11-10")


@patch.object(config, "BOT_ENABLED", True)
@patch.object(config, "DAILY_SUBSCRIPTIONS_ENABLED", True)
@patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
@patch.object(config, "CHANNEL_JOIN_URL", "")
class SubscriptionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(config.LOCAL_TZ).replace(hour=10, minute=0)
        self.report = {"version": 1, "generated_at": self.now.isoformat(),
                       "messages": [{"text": "Card", "image_url": ""}]}
        # Fixed timestamps avoid dependence on the wall clock's report freshness window.
        self.validation = patch("src.member_bot.validate_report", side_effect=lambda value: value)
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.state = subscribers.Subscribers()
        self.bot = member_bot.MemberBot(self.report, clock=lambda: 0, subscribers=self.state)
        self.api = patch("src.member_bot.telegram.call_api", return_value={"status": "member"}).start()
        self.send = patch("src.member_bot.telegram.send_message").start()
        self.cards = patch("src.member_bot.telegram.send_illustrated_message").start()
        self.addCleanup(patch.stopall)

    def test_start_subscribes_but_report_does_not(self):
        self.bot.handle_update(request("/report", 99))
        self.assertNotIn("99", self.state.users)
        self.bot.handle_update(request("/start"))
        self.assertIn("42", self.state.users)
        self.assertEqual(self.state.users["42"]["last_daily"], self.now.date().isoformat())

    def test_stop_requires_no_membership_and_does_not_resubscribe_on_report(self):
        self.state.subscribe(42)
        self.bot.handle_update(request("/stop"))
        self.assertNotIn("42", self.state.users)
        self.api.assert_not_called()
        self.bot.handle_update(request("/report"))
        self.assertNotIn("42", self.state.users)

    def test_nonmember_start_only_subscribes_after_successful_recheck(self):
        self.api.return_value = {"status": "left"}
        self.bot.handle_update(request("/start"))
        self.assertFalse(self.state.users)
        buttons = self.send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[1][0]["callback_data"], "subscribe_after_join")
        self.api.return_value = {"status": "member"}
        self.bot.handle_update({"callback_query": {"id": "join", "data": "subscribe_after_join",
            "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"}}}})
        self.assertIn("42", self.state.users)
        self.cards.assert_called_once()

    def test_daily_delivery_is_deduplicated_across_worker_instances(self):
        self.state.subscribe(42)
        self.bot.broadcast(now=self.now)
        second = member_bot.MemberBot(self.report, clock=lambda: 0, subscribers=self.state)
        second.broadcast(now=self.now)
        self.cards.assert_called_once()
        self.assertEqual(self.bot.reports_completed, 1)

    def test_yesterday_and_before_seven_are_not_broadcast(self):
        self.state.subscribe(42)
        self.bot.broadcast(now=self.now.replace(hour=6))
        self.bot.broadcast(now=self.now + timedelta(days=1))
        self.api.assert_not_called()
        self.cards.assert_not_called()

    def test_nonmember_or_membership_failure_gets_no_automatic_messages(self):
        self.state.subscribe(42)
        self.api.return_value = {"status": "left"}
        self.bot.broadcast(now=self.now)
        self.api.side_effect = telegram.TelegramError("temporary")
        self.bot.broadcast(now=self.now)
        self.send.assert_not_called()
        self.cards.assert_not_called()
        self.assertIsNone(self.state.users["42"]["last_daily"])

    def test_blocked_subscriber_removed_and_next_subscriber_receives_report(self):
        self.state.subscribe(42)
        self.state.subscribe(99)
        self.send.side_effect = [telegram.RecipientUnavailableError("blocked", chat_id=42), None]
        self.bot.broadcast(now=self.now)
        self.assertNotIn("42", self.state.users)
        self.assertEqual(self.state.users["99"]["last_daily"], self.now.date().isoformat())
        self.cards.assert_called_once()

    def test_failed_or_partial_delivery_not_marked_complete(self):
        self.state.subscribe(42)
        self.cards.side_effect = telegram.TelegramError("uncertain")
        self.bot.broadcast(now=self.now)
        self.assertIsNone(self.state.users["42"]["last_daily"])
        self.cards.side_effect = None
        self.bot.deadline = 20
        self.bot.broadcast(now=self.now)
        self.assertIsNone(self.state.users["42"]["last_daily"])

    def test_pause_and_daily_toggle_prevent_broadcast(self):
        self.state.subscribe(42)
        for name in ("BOT_ENABLED", "DAILY_SUBSCRIPTIONS_ENABLED"):
            with patch.object(config, name, False):
                self.bot.broadcast(now=self.now)
        self.api.assert_not_called()

    def test_private_delivery_does_not_use_admin_destination(self):
        self.state.subscribe(99)
        targets = []
        self.cards.side_effect = lambda *args: targets.append(telegram._resolve_target())
        with patch.object(config, "TELEGRAM_USER_ID", "42"):
            self.bot.broadcast(now=self.now)
        self.assertEqual(targets, ["99"])

    def test_backlog_beyond_worker_limit_remains_pending(self):
        self.state.subscribe(42)
        self.state.subscribe(99)
        self.bot.max_reports = 1
        self.bot.broadcast(now=self.now)
        self.assertIsNotNone(self.state.users["42"]["last_daily"])
        self.assertIsNone(self.state.users["99"]["last_daily"])

    def test_subscription_survives_a_new_process_and_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "TELEGRAM_BOT_TOKEN", "test"), patch.object(config, "SUBSCRIBER_STATE_KEY", ""):
            path = Path(directory) / "state.enc"
            self.bot.handle_update(request("/start"))
            self.state.save(path)
            restored = subscribers.Subscribers.load(path)
            bot = member_bot.MemberBot(self.report, clock=lambda: 0, subscribers=restored)
            bot.handle_update(request("/stop"))
            restored.save(path)
            self.assertFalse(subscribers.Subscribers.load(path).users)


class WorkerLifecycleTests(unittest.TestCase):
    @patch.object(config, "BOT_ENABLED", True)
    @patch("src.member_bot.Subscribers.load")
    @patch("src.member_bot.load_report")
    @patch("src.member_bot.MemberBot")
    def test_pending_stop_commands_processed_before_broadcast_and_state_saved(self, factory, load_report, load_state):
        state = subscribers.Subscribers()
        state.save = Mock()
        load_state.return_value = state
        order = []
        bot = factory.return_value
        bot.process_pending.side_effect = lambda **kwargs: order.append("requests")
        bot.broadcast.side_effect = lambda: order.append("broadcast")
        with patch("sys.argv", ["member_bot", "--state", "in.enc", "--save-state", "out.enc", "--broadcast"]):
            self.assertEqual(member_bot.main(), 0)
        self.assertEqual(order, ["requests", "broadcast"])
        state.save.assert_called_with("out.enc")

    @patch.object(config, "BOT_ENABLED", True)
    @patch("src.member_bot.Subscribers.load", side_effect=ValueError("Cannot decrypt subscriber state"))
    @patch("src.member_bot.MemberBot")
    def test_unreadable_state_never_starts_polling_or_replaces_state(self, factory, load):
        with patch("sys.argv", ["member_bot", "--state", "in.enc", "--save-state", "out.enc", "--broadcast"]):
            self.assertEqual(member_bot.main(), 1)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
