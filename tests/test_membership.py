"""Member access, private recipient isolation, cache validation and update offsets."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from src import config, main, member_bot, report_store, telegram_sender as telegram


def snapshot():
    return {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "messages": [{"text": "گزارش مخصوص اعضا", "image_url": "https://example.org/photo.jpg"}]}


def update(user_id=42, text="/report", update_id=10, chat_type="private"):
    return {"update_id": update_id, "message": {"chat": {"id": user_id, "type": chat_type},
                                               "from": {"id": user_id, "is_bot": False}, "text": text}}


@patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
@patch.object(config, "CHANNEL_JOIN_URL", "")
class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.bot = member_bot.MemberBot(snapshot(), clock=lambda: 0)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.api = self.stack.enter_context(patch("src.member_bot.telegram.call_api", return_value={"status": "member"}))
        self.send = self.stack.enter_context(patch("src.member_bot.telegram.send_message"))
        self.cards = self.stack.enter_context(patch("src.member_bot.telegram.send_illustrated_message"))

    def test_allowed_membership_statuses(self):
        for status in ("member", "administrator", "creator"):
            self.assertTrue(member_bot.is_member({"status": status}))
        self.assertTrue(member_bot.is_member({"status": "restricted", "is_member": True}))
        for value in ({"status": "left"}, {"status": "kicked"}, {"status": "restricted", "is_member": False},
                      {"status": "restricted", "is_member": "true"}, {"status": "unknown"}, {}, None):
            self.assertFalse(member_bot.is_member(value))

    def test_member_gets_report_for_start_and_report(self):
        for command in ("/start", "/report", "/run"):
            self.bot.served.clear()
            self.bot.handle_update(update(text=command))
        self.assertEqual(self.cards.call_count, 3)
        self.assertEqual(self.api.call_args.args, ("getChatMember", {"chat_id": "@sentencedIntoMusic", "user_id": 42}))

    def test_nonmember_gets_join_and_recheck_buttons_without_report(self):
        self.api.return_value = {"status": "left"}
        self.bot.handle_update(update())
        self.cards.assert_not_called()
        buttons = self.send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["url"], "https://t.me/sentencedIntoMusic")
        self.assertEqual(buttons[1][0]["callback_data"], "check_membership")

    def test_membership_api_error_fails_closed(self):
        self.api.side_effect = telegram.TelegramError("temporary error")
        self.bot.handle_update(update())
        self.cards.assert_not_called()
        self.assertIn("بررسی عضویت فعلاً ممکن نیست", self.send.call_args.args[0])

    def test_recheck_after_joining_grants_access(self):
        self.api.side_effect = [{"status": "left"}, True, {"status": "member"}]
        self.bot.handle_update(update())
        self.cards.assert_not_called()
        self.bot.handle_update({"callback_query": {"id": "callback", "data": "check_membership", "from": {"id": 42},
                                                   "message": {"chat": {"id": 42, "type": "private"}}}})
        self.cards.assert_called_once()
        self.assertEqual(self.api.call_args_list[1].args[0], "answerCallbackQuery")

    def test_leaving_channel_is_checked_again_on_next_request(self):
        self.api.side_effect = [{"status": "member"}, {"status": "left"}]
        self.bot.handle_update(update())
        self.bot.handle_update(update(update_id=11))
        self.cards.assert_called_once()
        self.assertIn("ابتدا عضو کانال", self.send.call_args.args[0])

    def test_duplicate_user_requests_do_not_repeat_report_in_one_run(self):
        self.bot.handle_update(update())
        self.bot.handle_update(update(update_id=11))
        self.cards.assert_called_once()
        self.assertEqual(self.api.call_count, 2)

    def test_group_and_mismatched_identity_requests_are_ignored(self):
        self.bot.handle_update(update(chat_type="group"))
        spoofed = update()
        spoofed["message"]["chat"]["id"] = 99
        self.bot.handle_update(spoofed)
        self.api.assert_not_called()
        self.send.assert_not_called()
        self.cards.assert_not_called()

    def test_command_addressed_to_different_bot_is_ignored(self):
        self.bot.username = "ourbot"
        self.bot.handle_update(update(text="/report@otherbot"))
        self.api.assert_not_called()
        self.bot.handle_update(update(text="/report@OurBot"))
        self.cards.assert_called_once()

    def test_missing_snapshot_does_not_generate_content(self):
        self.bot.report = None
        self.bot.handle_update(update())
        self.cards.assert_not_called()
        self.assertIn("هنوز آماده نیست", self.send.call_args.args[0])

    def test_runtime_limit_stops_partial_delivery(self):
        self.bot.deadline = 0
        self.bot.handle_update(update())
        self.cards.assert_not_called()
        self.assertIn("کامل نشد", self.send.call_args.args[0])

    def test_expired_callback_ack_still_checks_membership(self):
        self.api.side_effect = [telegram.TelegramError("expired"), {"status": "member"}]
        self.bot.handle_update({"callback_query": {"id": "old", "data": "report", "from": {"id": 42},
                                                   "message": {"chat": {"id": 42, "type": "private"}}}})
        self.cards.assert_called_once()

    def test_help_does_not_reveal_report_content(self):
        self.bot.handle_update(update(text="/help"))
        self.api.assert_not_called()
        self.cards.assert_not_called()
        self.assertNotIn("گزارش مخصوص اعضا", self.send.call_args.args[0])


@patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
@patch.object(config, "CHANNEL_JOIN_URL", "")
class PollingTests(unittest.TestCase):
    @patch.object(config, "TELEGRAM_BOT_TOKEN", "test")
    @patch("src.member_bot.telegram.call_api")
    def test_startup_verifies_admin_and_keeps_existing_webhook(self, api):
        bot = member_bot.MemberBot()
        api.return_value = {"url": "https://existing.example/webhook"}
        with self.assertRaisesRegex(ValueError, "empty webhook"):
            bot.check_setup()
        self.assertEqual(api.call_count, 1)
        api.reset_mock()
        api.side_effect = [{"url": ""}, {"id": 9, "username": "OurBot"}, {"status": "member"}]
        with self.assertRaisesRegex(ValueError, "administrator"):
            bot.check_setup()
        self.assertNotIn("getUpdates", [call.args[0] for call in api.call_args_list])

    @patch.object(config, "TELEGRAM_BOT_TOKEN", "test")
    @patch("src.member_bot.telegram.call_api")
    def test_admin_setup_succeeds(self, api):
        api.side_effect = [{"url": ""}, {"id": 9, "username": "OurBot"}, {"status": "administrator"}]
        bot = member_bot.MemberBot()
        bot.check_setup()
        self.assertEqual(bot.username, "ourbot")

    @patch("src.member_bot.telegram.call_api")
    def test_confirm_only_processed_prefix_when_batch_is_limited(self, api):
        bot = member_bot.MemberBot(snapshot(), clock=lambda: 0, max_reports=1)
        bot.check_setup = Mock()
        def handle(value):
            bot.report_count += 1
        bot.handle_update = Mock(side_effect=handle)
        api.side_effect = [[update(update_id=10), update(user_id=99, update_id=11)], [update(user_id=99, update_id=11)]]
        bot.process_pending()
        bot.handle_update.assert_called_once()
        self.assertEqual(api.call_args.args[1]["offset"], 11)
        self.assertEqual(api.call_count, 2)

    @patch("src.member_bot.telegram.call_api")
    def test_delivery_error_does_not_block_later_updates(self, api):
        bot = member_bot.MemberBot(clock=lambda: 0)
        bot.check_setup = Mock()
        bot.handle_update = Mock(side_effect=[telegram.TelegramError("blocked"), None])
        api.side_effect = [[update(update_id=10), update(user_id=99, update_id=11)], [], []]
        bot.process_pending()
        self.assertEqual(bot.handle_update.call_count, 2)
        self.assertEqual(api.call_args.args[1]["offset"], 12)

    @patch("src.member_bot.telegram.call_api")
    def test_deadline_does_not_acknowledge_unprocessed_updates(self, api):
        bot = member_bot.MemberBot(clock=lambda: 0)
        bot.check_setup = Mock()
        bot.handle_update = Mock()
        bot.deadline = 0
        api.return_value = [update()]
        bot.process_pending()
        bot.handle_update.assert_not_called()
        self.assertEqual(api.call_count, 1)

    def test_private_channel_requires_explicit_join_link(self):
        with patch.object(config, "MEMBERSHIP_CHANNEL_ID", "-10012345"):
            with self.assertRaisesRegex(ValueError, "CHANNEL_JOIN_URL"):
                member_bot.join_url()
            with patch.object(config, "CHANNEL_JOIN_URL", "https://t.me/+invite"):
                self.assertEqual(member_bot.join_url(), "https://t.me/+invite")
        with patch.object(config, "CHANNEL_JOIN_URL", "https://example.org/unrelated"):
            with self.assertRaises(ValueError):
                member_bot.join_url()


class RecipientTests(unittest.TestCase):
    @patch.object(config, "TELEGRAM_CHANNEL_ID", "@scheduled_channel")
    @patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
    @patch.object(config, "CHANNEL_JOIN_URL", "")
    @patch("src.member_bot.telegram.call_api", return_value={"status": "member"})
    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_member_content_never_leaks_into_configured_channel(self, post, api):
        bot = member_bot.MemberBot(snapshot(), clock=lambda: 0)
        bot.handle_update(update())
        self.assertTrue(post.call_args_list)
        for call in post.call_args_list:
            self.assertEqual(json.loads(call.args[1])["chat_id"], "42")
        self.assertEqual(telegram._resolve_target(), "@scheduled_channel")
        self.assertEqual(config.TELEGRAM_CHANNEL_ID, "@scheduled_channel")

    @patch.object(config, "TELEGRAM_CHANNEL_ID", "@scheduled_channel")
    def test_nested_target_context_restores_after_exception(self):
        with telegram.delivery_target(42):
            self.assertEqual(telegram._resolve_target(), "42")
            with self.assertRaises(RuntimeError):
                with telegram.delivery_target(99):
                    self.assertEqual(telegram._resolve_target(), "99")
                    raise RuntimeError()
            self.assertEqual(telegram._resolve_target(), "42")
        self.assertEqual(telegram._resolve_target(), "@scheduled_channel")

    @patch.object(config, "TELEGRAM_CHANNEL_ID", "@channel")
    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_join_keyboard_is_attached_to_personal_reply(self, post):
        with telegram.delivery_target(42):
            telegram.send_message("Join", reply_markup={"inline_keyboard": [[{"text": "Join", "url": "https://t.me/testchannel"}]]})
        payload = json.loads(post.call_args.args[1])
        self.assertEqual(payload["chat_id"], "42")
        self.assertIn("inline_keyboard", payload["reply_markup"])


class ReportStoreTests(unittest.TestCase):
    def test_snapshot_round_trip_whitelists_only_public_fields(self):
        report = snapshot()
        report["credentials"] = "do not save"
        report["messages"][0]["user_id"] = 42
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            report_store.save_report(path, report)
            loaded = report_store.load_report(path)
        self.assertNotIn("credentials", loaded)
        self.assertNotIn("user_id", loaded["messages"][0])
        self.assertEqual(loaded["messages"][0]["text"], "گزارش مخصوص اعضا")

    def test_stale_future_naive_and_malformed_reports_rejected(self):
        for stamp in ((datetime.now(timezone.utc) - timedelta(hours=37)).isoformat(),
                      (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "2026-11-03T07:00:00", "not-a-date"):
            report = snapshot()
            report["generated_at"] = stamp
            with self.assertRaises(ValueError):
                report_store.validate_report(report)
        for value in ({}, [], {**snapshot(), "messages": []}, {**snapshot(), "messages": [{"text": 42}]}):
            with self.assertRaises(ValueError):
                report_store.validate_report(value)

    def test_large_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_bytes(b" " * (report_store.MAX_SNAPSHOT_BYTES + 1))
            with self.assertRaises(ValueError):
                report_store.load_report(path)

    @patch.object(config, "TELEGRAM_BOT_TOKEN", "")
    @patch("src.main.build_report")
    @patch("src.main.telegram_sender.send_illustrated_message")
    def test_preview_can_seed_snapshot_without_telegram_credentials(self, send, build):
        build.return_value = (snapshot(), None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily-report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.run(dry_run=True, export_report=path), 0)
            self.assertEqual(report_store.load_report(path)["messages"], snapshot()["messages"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
