import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
from zoneinfo import ZoneInfo
from src import config, formatting, http_client, main, news, song, sports, telegram_sender, weather, writer

NOW = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
ITEM = {"title": "Original headline", "summary": "Source summary", "link": "https://example.org/story",
        "source": "Example", "published_at": NOW.isoformat()}


def response(data):
    result = Mock()
    result.read.return_value = json.dumps(data).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def rss_item(title="Headline", link="https://example.org/story", date="Mon, 02 Nov 2026 03:00:00 GMT"):
    return f"<item><title>{title}</title><link>{link}</link><description>&lt;p&gt;A &amp;amp; B&lt;/p&gt;</description><pubDate>{date}</pubDate></item>"


class FormattingTests(unittest.TestCase):
    def test_utf16_offset_for_persian_and_emoji(self):
        text, entities = formatting.render("🎵 **سلام** و `x & y` <unsafe>")
        self.assertEqual(text, "🎵 سلام و x & y <unsafe>")
        self.assertEqual(entities[0], {"type": "bold", "offset": 3, "length": 4})
        self.assertEqual(entities[1]["type"], "code")

    def test_long_messages_reassemble_and_preserve_spans(self):
        source = "آغاز\n\n**" + "الف🎉" * 2500 + "**\n```python\nprint('<tag>')\n```\nپایان"
        expected, _ = formatting.render(source)
        chunks = list(formatting.split_message(source))
        self.assertGreater(len(chunks), 2)
        self.assertEqual("".join(text for text, _ in chunks), expected)
        for text, entities in chunks:
            length = formatting.utf16_length(text)
            self.assertLessEqual(length, 4000)
            for entity in entities:
                self.assertGreater(entity["length"], 0)
                self.assertLessEqual(entity["offset"] + entity["length"], length)
        self.assertTrue(any(entity["type"] == "pre" for _, entities in chunks for entity in entities))

    def test_code_is_not_interpreted_as_bold(self):
        text, entities = formatting.render("```python\nx = '**literal**'\n``` **واقعی**")
        self.assertIn("**literal**", text)
        self.assertEqual([item["type"] for item in entities], ["pre", "bold"])

    def test_empty_and_unmatched_markdown_are_safe(self):
        self.assertEqual(list(formatting.split_message("")), [])
        self.assertEqual(formatting.render("**open <b>&")[0], "**open <b>&")


class NewsTests(unittest.TestCase):
    @patch.dict(news.FEEDS, {"test": ("Source", "https://example.org/feed")}, clear=True)
    @patch("src.news.request_bytes")
    def test_dates_duplicates_html_and_order(self, fetch):
        feed = (rss_item() + rss_item() + rss_item(title="Old", link="https://example.org/old", date="Sat, 31 Oct 2026 03:00:00 GMT")
                + rss_item(title="Newer", link="https://example.org/new", date="Mon, 02 Nov 2026 04:00:00 GMT")
                + rss_item(title="Future", link="https://example.org/future", date="Tue, 03 Nov 2026 04:00:00 GMT")
                + rss_item(title="No date", date=""))
        fetch.return_value = f"<rss><channel>{feed}</channel></rss>".encode()
        result = news.get_diverse_news(NOW)["test"]
        self.assertEqual([item["title"] for item in result], ["Newer", "Headline"])
        self.assertEqual(result[0]["summary"], "A & B")
        self.assertEqual(result[0]["source"], "Source")

    @patch.dict(news.FEEDS, {"test": ("Source", "https://example.org/feed")}, clear=True)
    @patch("src.news.request_bytes", side_effect=TimeoutError)
    def test_feed_failure_does_not_crash(self, fetch):
        self.assertEqual(news.get_diverse_news(NOW), {})

    def test_unsafe_links(self):
        for link in ("javascript:alert(1)", "https://user:pass@example.com", "https://host/a\nb", "ftp://example.com"):
            self.assertEqual(news.safe_url(link), "")
        self.assertEqual(news.safe_url("https://example.org/a#fragment"), "https://example.org/a")


@patch.object(config, "LOCAL_TZ", ZoneInfo("Asia/Tehran"))
class WriterTests(unittest.TestCase):
    @patch("src.writer._call_groq", return_value=[{"id": "n1", "title": "تیتر", "summary": "خلاصه", "link": "https://invented.test"}])
    def test_source_metadata_is_owned_by_code(self, generate):
        text = writer.elaborate_news({"جهان": [ITEM]})
        self.assertIn(ITEM["link"], text)
        self.assertIn("Example", text)
        self.assertIn("08:00", text)
        self.assertNotIn("invented.test", text)
        self.assertNotIn(ITEM["link"], generate.call_args.args[0])

    @patch("src.writer._call_groq", return_value=[{"id": "n1", "title": "x", "summary": "y"}, {"id": "n1", "title": "a", "summary": "b"}])
    def test_duplicate_records_fall_back(self, generate):
        text = writer.elaborate_news({"جهان": [ITEM]})
        self.assertIn("Original headline", text)
        self.assertIn("متن اصلی منبع", text)

    def test_reject_unknown_oversized_and_invalid_records(self):
        result = writer._validated_records(
            [{"id": []}, {"id": "unknown", "title": "ok"}, {"id": "n1", "title": "x" * 101}],
            {"n1": ITEM}, {"title": 100})
        self.assertEqual(result, {})

    @patch.object(config, "GROQ_API_KEY", "")
    @patch("src.writer.request_json")
    def test_without_key_no_model_call_and_grounded_fallback(self, fetch):
        self.assertIn("Original headline", writer.elaborate_news({"جهان": [ITEM]}))
        self.assertIn("تمرین امروز", writer.generate_daily_insight(NOW))
        fetch.assert_not_called()

    @patch.object(config, "GROQ_API_KEY", "test-key")
    @patch.object(config, "GROQ_MODELS", ["first", "second"])
    @patch("src.writer.request_json")
    def test_truncated_or_invalid_model_response_uses_next_model(self, fetch):
        fetch.side_effect = [
            {"choices": [{"finish_reason": "length", "message": {"content": '{"items": []}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"items": []}'}}]},
        ]
        self.assertEqual(writer._call_groq("test"), [])
        self.assertEqual(fetch.call_count, 2)
        payload = json.loads(fetch.call_args.kwargs["data"])
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    @patch("src.writer._call_groq", return_value=None)
    def test_insight_rotation_is_stable_for_same_day(self, generate):
        self.assertEqual(writer.generate_daily_insight(NOW), writer.generate_daily_insight(NOW))
        self.assertNotEqual(writer.generate_daily_insight(NOW), writer.generate_daily_insight(NOW + timedelta(days=1)))


class TelegramTests(unittest.TestCase):
    @patch.object(config, "TELEGRAM_USER_ID", "123")
    @patch.object(config, "TELEGRAM_CHANNEL_ID", "")
    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_send_uses_entities_and_does_not_truncate(self, post):
        source = "**" + "خبر🎵" * 2000 + "**"
        telegram_sender.send_message(source)
        payloads = [json.loads(call.args[1]) for call in post.call_args_list]
        self.assertEqual("".join(p["text"] for p in payloads), formatting.render(source)[0])
        self.assertTrue(all("parse_mode" not in p and p["entities"] for p in payloads))

    @patch("src.telegram_sender.time.sleep")
    @patch("src.telegram_sender.urllib.request.urlopen")
    def test_rate_limit_retries_with_server_delay(self, fetch, sleep):
        error = urllib.error.HTTPError("https://redacted", 429, "limited", {}, io.BytesIO(b'{"parameters":{"retry_after":2}}'))
        fetch.side_effect = [error, response({"ok": True})]
        self.assertTrue(telegram_sender._post("sendMessage", b"{}", "application/json")["ok"])
        self.assertEqual(fetch.call_count, 2)
        self.assertIn(unittest.mock.call(2), sleep.call_args_list)

    @patch("src.telegram_sender.time.sleep")
    @patch("src.telegram_sender.urllib.request.urlopen", side_effect=urllib.error.URLError("secret-token-url"))
    def test_ambiguous_network_errors_are_not_retried_or_leaked(self, fetch, sleep):
        with self.assertRaises(telegram_sender.TelegramError) as error:
            telegram_sender._post("sendMessage", b"{}", "application/json")
        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn("secret-token", str(error.exception))

    @patch("src.telegram_sender.time.sleep")
    @patch("src.telegram_sender.urllib.request.urlopen")
    def test_auth_errors_do_not_retry(self, fetch, sleep):
        fetch.side_effect = urllib.error.HTTPError("redacted", 401, "unauthorized", {}, io.BytesIO(b"{}"))
        with self.assertRaises(telegram_sender.TelegramError):
            telegram_sender._post("sendMessage", b"{}", "application/json")
        self.assertEqual(fetch.call_count, 1)

    @patch("src.telegram_sender.time.sleep")
    @patch("src.telegram_sender.urllib.request.urlopen")
    def test_malformed_api_result_is_a_controlled_delivery_error(self, fetch, sleep):
        fetch.return_value = response([])
        with self.assertRaises(telegram_sender.TelegramError):
            telegram_sender._post("sendMessage", b"{}", "application/json")
        self.assertEqual(fetch.call_count, 1)

    def test_audio_size_and_caption_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.mp3"
            path.write_bytes(b"audio")
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.send_audio(str(path), "🎵" * 513)
            path.write_bytes(b"")
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.send_audio(str(path))


@patch.object(config, "LOCAL_TZ", ZoneInfo("Asia/Tehran"))
class WeatherSportsMusicTests(unittest.TestCase):
    @patch.object(config, "OPENWEATHER_API_KEY", "test")
    @patch("src.weather.request_json")
    def test_forecast_failure_preserves_current_weather(self, fetch):
        fetch.side_effect = [{"main": {"temp": 20, "humidity": 30}, "weather": [{"description": "صاف"}], "wind": {"speed": 2}}, TimeoutError]
        result = weather.get_mashhad_weather()
        self.assertEqual(result["current"]["temp"], 20)
        self.assertEqual(result["forecast"], [])

    @patch.object(config, "OPENWEATHER_API_KEY", "test")
    @patch("src.weather.datetime")
    @patch("src.weather.request_json")
    def test_forecast_uses_local_time_and_future_slots(self, fetch, clock):
        clock.now.return_value = NOW.astimezone(config.LOCAL_TZ)
        clock.fromtimestamp.side_effect = datetime.fromtimestamp
        current = {"main": {"temp": "20", "humidity": 30}, "weather": [{"description": "صاف"}], "wind": {"speed": 2}}
        slots = [{"dt": (NOW + timedelta(hours=delta)).timestamp(), "main": {"temp": 21},
                  "weather": [{"description": "صاف"}]} for delta in (-1, 1, 4, 7, 10, 13)]
        fetch.side_effect = [current, {"list": slots}]
        result = weather.get_mashhad_weather()
        self.assertEqual(len(result["forecast"]), 4)
        self.assertIn("09:00", result["forecast"][0])
        self.assertEqual(result["current"]["temp"], 20.0)

    @patch.object(config, "SPORTRADAR_SOCCER_API_KEY", "test")
    @patch.object(config, "SPORTRADAR_BASKETBALL_API_KEY", "")
    @patch("src.sports.request_json")
    def test_sports_local_day_final_status_team_qualifiers_and_deduplication(self, fetch):
        def event(key, status, start):
            return {"sport_event": {"id": key, "start_time": start, "competitors": [
                {"name": "Away", "qualifier": "away"}, {"name": "Home", "qualifier": "home"}]},
                "sport_event_status": {"status": status, "home_score": 0, "away_score": 2}}
        valid = event("valid", "closed", "2026-10-31T22:00:00Z")  # Nov 1 in Tehran
        fetch.return_value = {"summaries": [valid, event("live", "live", "2026-11-01T12:00:00Z"),
                                             event("wrong-day", "ended", "2026-11-01T23:00:00Z")]}
        self.assertEqual(sports.get_sports_results(NOW)["فوتبال"], ["Home 0 – 2 Away"])
        self.assertEqual(fetch.call_count, 2)

    @patch.object(config, "SPOTIFY_CLIENT_ID", "test")
    @patch.object(config, "SPOTIFY_CLIENT_SECRET", "test")
    @patch("src.song.request_json")
    def test_spotify_selects_track_not_album(self, fetch):
        fetch.side_effect = [{"access_token": "token"}, {"albums": {"items": [{"id": "album"}]}},
                             {"items": [{"name": "Actual song", "artists": [{"name": "Artist"}],
                                         "external_urls": {"spotify": "https://open.spotify.com/track/1"}}]}]
        self.assertEqual(song.get_trending_song()["title"], "Actual song")
        self.assertIn("albums/album/tracks", fetch.call_args.args[0])

    @patch.object(config, "ENABLE_AUDIO_DOWNLOAD", True)
    @patch("src.song.subprocess.run", side_effect=FileNotFoundError)
    def test_missing_downloader_is_nonfatal(self, process):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(song.download_audio("Artist - Song", directory))
        self.assertEqual(process.call_args.kwargs["timeout"], 180)

    @patch.object(config, "ENABLE_AUDIO_DOWNLOAD", False)
    @patch("src.song.subprocess.run")
    def test_download_disabled_by_default(self, process):
        self.assertIsNone(song.download_audio("test", "/tmp"))
        process.assert_not_called()


class HttpTests(unittest.TestCase):
    @patch("src.http_client.time.sleep")
    @patch("src.http_client.urllib.request.urlopen")
    def test_transient_get_errors_retry(self, fetch, sleep):
        fetch.side_effect = [urllib.error.HTTPError("redacted", 503, "busy", {}, io.BytesIO(b"")), response({"ok": True})]
        self.assertEqual(http_client.request_json("https://example.org"), {"ok": True})
        self.assertEqual(fetch.call_count, 2)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("src.main.weather.get_mashhad_weather", side_effect=TimeoutError))
        self.stack.enter_context(patch("src.main.news.get_diverse_news", return_value={"جهان": [ITEM]}))
        self.stack.enter_context(patch("src.main.writer.prepare_news_cards", return_value=[{"text": "خبر نمونه", "image_url": "https://example.org/news.jpg"}]))
        self.stack.enter_context(patch("src.main.sports.get_sports_results", return_value={}))
        self.stack.enter_context(patch("src.main.song.get_trending_song", return_value=None))
        self.stack.enter_context(patch("src.main.song.get_apple_music_cards", return_value=[{"text": "موسیقی", "image_url": "https://example.org/cover.jpg"}]))
        self.stack.enter_context(patch("src.main.writer.generate_daily_insight", return_value="یادگیری"))
        self.send = self.stack.enter_context(patch("src.main.telegram_sender.send_illustrated_message"))
        self.audio = self.stack.enter_context(patch("src.main.song.download_audio"))

    def test_dry_run_without_credentials_never_sends_or_downloads(self):
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.run(dry_run=True), 0)
        self.assertIn("خبر نمونه", output.getvalue())
        self.assertIn("یادگیری", output.getvalue())
        self.send.assert_not_called()
        self.audio.assert_not_called()

    def test_delivery_failure_does_not_skip_later_sections(self):
        self.send.side_effect = [telegram_sender.TelegramError("failed"), [], [], []]
        with patch.object(config, "TELEGRAM_BOT_TOKEN", "test"), patch.object(config, "TELEGRAM_USER_ID", "123"):
            self.assertEqual(main.run(), 1)
        self.assertEqual(self.send.call_count, 4)

    def test_missing_delivery_configuration_fails_early(self):
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""):
            with self.assertRaises(ValueError):
                main.run()
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
