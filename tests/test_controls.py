"""Global pause controls and permanent recipient failure handling."""
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error
from src import config, main, member_bot, telegram_sender as telegram


class GlobalPauseTests(unittest.TestCase):
    @patch.object(config, "BOT_ENABLED", False)
    @patch("src.main.build_report")
    @patch("src.main.config.validate")
    @patch("src.main.telegram_sender.send_illustrated_message")
    def test_daily_pause_prevents_collection_and_delivery(self, send, validate, build):
        self.assertEqual(main.run(), 0)
        send.assert_not_called()
        validate.assert_not_called()
        build.assert_not_called()

    @patch.object(config, "BOT_ENABLED", False)
    @patch("src.member_bot.MemberBot")
    @patch("src.member_bot.load_report")
    @patch("src.member_bot.logging.basicConfig")
    @patch("sys.argv", ["member_bot"])
    def test_member_cli_pause_does_not_read_snapshot_or_contact_telegram(self, logging_config, load, bot):
        self.assertEqual(member_bot.main(), 0)
        load.assert_not_called()
        bot.assert_not_called()

    @patch.object(config, "BOT_ENABLED", False)
    @patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
    @patch.object(config, "CHANNEL_JOIN_URL", "")
    @patch("src.member_bot.telegram.call_api")
    def test_direct_polling_pause_does_not_consume_updates(self, api):
        bot = member_bot.MemberBot()
        bot.check_setup = Mock()
        bot.process_pending()
        bot.check_setup.assert_not_called()
        api.assert_not_called()


class RecipientFailureTests(unittest.TestCase):
    @patch("src.telegram_sender.time.sleep")
    @patch("src.telegram_sender.urllib.request.urlopen")
    def test_delivery_403_preserves_recipient_without_retry_or_secret_logging(self, fetch, sleep):
        for method in ("sendMessage", "sendPhoto", "sendAudio"):
            fetch.reset_mock()
            fetch.side_effect = urllib.error.HTTPError(
                "https://redacted", 403, "Forbidden", {}, io.BytesIO(b'{"description":"bot was blocked by the user"}'))
            with telegram.delivery_target(42):
                with self.assertRaises(telegram.RecipientUnavailableError) as error:
                    telegram._post(method, b"{}", "application/json")
            self.assertEqual(error.exception.chat_id, "42")
            self.assertNotIn("redacted", str(error.exception))
            self.assertEqual(fetch.call_count, 1)

    @patch("src.telegram_sender.urllib.request.urlopen")
    def test_membership_403_remains_api_error_and_not_blocked_recipient(self, fetch):
        fetch.side_effect = urllib.error.HTTPError("redacted", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        with self.assertRaises(telegram.TelegramError) as error:
            telegram.call_api("getChatMember", {"chat_id": "@channel", "user_id": 42})
        self.assertNotIsInstance(error.exception, telegram.RecipientUnavailableError)

    @patch.object(config, "TELEGRAM_USER_ID", "42")
    @patch.object(config, "TELEGRAM_CHANNEL_ID", "")
    @patch("src.telegram_sender._post", side_effect=telegram.RecipientUnavailableError("blocked", chat_id=42))
    def test_forbidden_photo_does_not_attempt_text_fallback(self, post):
        with self.assertRaises(telegram.RecipientUnavailableError):
            telegram.send_illustrated_message("Caption", "https://example.org/photo.jpg")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "sendPhoto")

    @patch.object(config, "BOT_ENABLED", True)
    @patch.object(config, "TELEGRAM_BOT_TOKEN", "test")
    @patch.object(config, "TELEGRAM_USER_ID", "42")
    @patch.object(config, "ENABLE_AUDIO_DOWNLOAD", True)
    @patch("src.main.build_report")
    @patch("src.main.telegram_sender.send_illustrated_message")
    @patch("src.main.song.download_audio")
    def test_unavailable_daily_destination_skips_remaining_cards_and_audio(self, download, send, build):
        build.return_value = ({"messages": [{"text": "A", "image_url": ""}, {"text": "B", "image_url": ""}]},
                              {"title": "Track", "artist": "Artist"})
        send.side_effect = telegram.RecipientUnavailableError("blocked", chat_id=42)
        self.assertEqual(main.run(), 1)
        send.assert_called_once()
        download.assert_not_called()


@patch.object(config, "BOT_ENABLED", True)
@patch.object(config, "MEMBERSHIP_CHANNEL_ID", "@sentencedIntoMusic")
@patch.object(config, "CHANNEL_JOIN_URL", "")
class BlockedQueueTests(unittest.TestCase):
    def test_skip_blocked_user_and_continue_next_user_in_same_batch(self):
        for blocked_method in ("sendMessage", "sendPhoto"):
            with self.subTest(blocked_method=blocked_method):
                report = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "messages": [
                    {"text": "A", "image_url": "https://example.org/a.jpg"},
                    {"text": "B", "image_url": "https://example.org/b.jpg"}]}
                bot = member_bot.MemberBot(report, clock=lambda: 0)
                bot.check_setup = Mock()
                def update(user, update_id):
                    return {"update_id": update_id, "message": {"chat": {"id": user, "type": "private"},
                                                               "from": {"id": user}, "text": "/report"}}
                updates = [update(42, 10), update(42, 11), update(99, 12)]
                def api(method, payload, **kwargs):
                    if method == "getUpdates":
                        return updates if "offset" not in payload else []
                    return {"status": "member"}
                def post(method, body, content_type, **kwargs):
                    payload = json.loads(body)
                    if payload["chat_id"] == "42" and method == blocked_method:
                        raise telegram.RecipientUnavailableError("blocked", chat_id=42)
                    return {"ok": True}
                with patch("src.member_bot.telegram.call_api", side_effect=api) as calls, \
                     patch("src.telegram_sender._post", side_effect=post) as deliveries:
                    bot.process_pending()
                self.assertEqual(bot.unreachable, {"42"})
                to_42 = [call.args[0] for call in deliveries.call_args_list if json.loads(call.args[1])["chat_id"] == "42"]
                to_99 = [call.args[0] for call in deliveries.call_args_list if json.loads(call.args[1])["chat_id"] == "99"]
                self.assertEqual(to_42, ["sendMessage"] if blocked_method == "sendMessage" else ["sendMessage", "sendPhoto"])
                self.assertEqual(to_99, ["sendMessage", "sendPhoto", "sendPhoto"])
                self.assertEqual([call.args[1]["user_id"] for call in calls.call_args_list if call.args[0] == "getChatMember"], [42, 99])
                self.assertEqual(calls.call_args.args[1]["offset"], 13)


if __name__ == "__main__":
    unittest.main()
