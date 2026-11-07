"""Image provenance, caption limits, and delivery failure regression tests."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from src import config, formatting, main, news, song, telegram_sender, writer


class FeedImageTests(unittest.TestCase):
    def entry(self, children):
        return ET.fromstring(f'<item xmlns:media="http://search.yahoo.com/mrss/">{children}</item>')

    def test_prefers_image_content_and_ignores_video(self):
        entry = self.entry('<media:content url="https://example.org/video.mp4" medium="video"/>'
                           '<media:thumbnail url="https://example.org/thumb.jpg"/>'
                           '<media:content url="https://example.org/main.jpg" type="image/jpeg"/>')
        self.assertEqual(news._feed_image(entry), "https://example.org/main.jpg")

    def test_largest_thumbnail_and_group_namespace(self):
        entry = self.entry('<media:group><media:thumbnail width="96" height="96" url="https://example.org/small.jpg"/>'
                           '<media:thumbnail width="640" height="360" url="https://example.org/large.jpg"/></media:group>')
        self.assertEqual(news._feed_image(entry), "https://example.org/large.jpg")

    def test_invalid_media_falls_back_to_image_enclosure(self):
        entry = self.entry('<media:thumbnail width="bad" url="javascript:alert(1)"/>'
                           '<enclosure type="audio/mpeg" url="https://example.org/audio.mp3"/>'
                           '<enclosure type="image/png" url="https://example.org/photo.png"/>')
        self.assertEqual(news._feed_image(entry), "https://example.org/photo.png")

    def test_missing_image_and_nonstring_urls(self):
        self.assertEqual(news._feed_image(self.entry('<title>No image</title>')), "")
        for value in (None, 123, {}, [], "file:///private/photo.png"):
            self.assertEqual(news.safe_url(value), "")

    @patch("src.writer._call_groq")
    def test_news_rewrites_retain_correct_source_image(self, generate):
        generate.return_value = [
            {"id": "n2", "title": "دوم", "summary": "خبر دوم", "image_url": "https://invented.org/fake.jpg"},
            {"id": "n1", "title": "اول", "summary": "خبر اول"},
        ]
        stories = [{"title": f"Story {number}", "summary": "Source", "link": f"https://example.org/{number}",
                    "source": "Example", "image_url": f"https://example.org/{number}.jpg"} for number in (1, 2)]
        cards = writer.prepare_news_cards({"جهان": stories})
        self.assertIn("اول", cards[0]["text"])
        self.assertEqual(cards[0]["image_url"], stories[0]["image_url"])
        self.assertIn("دوم", cards[1]["text"])
        self.assertEqual(cards[1]["image_url"], stories[1]["image_url"])
        self.assertNotIn("image_url", generate.call_args.args[0])
        self.assertNotIn("invented.org", str(cards))


class MusicArtworkTests(unittest.TestCase):
    @patch.object(config, "SPOTIFY_CLIENT_ID", "test")
    @patch.object(config, "SPOTIFY_CLIENT_SECRET", "test")
    @patch("src.song.request_json")
    def test_spotify_cover_comes_from_selected_album(self, fetch):
        fetch.side_effect = [{"access_token": "token"}, {"albums": {"items": [{"id": "album", "images": [
            {"url": "https://example.org/album.jpg"}, {"url": "https://example.org/small.jpg"}]}]}},
            {"items": [{"name": "Track", "artists": [{"name": "Artist"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/1"}}]}]
        result = song.get_trending_song()
        self.assertEqual(result["image_url"], "https://example.org/album.jpg")
        self.assertEqual(result["title"], "Track")

    @patch.object(config, "SPOTIFY_CLIENT_ID", "test")
    @patch.object(config, "SPOTIFY_CLIENT_SECRET", "test")
    @patch("src.song.request_json")
    def test_missing_spotify_art_does_not_drop_track(self, fetch):
        fetch.side_effect = [{"access_token": "token"}, {"albums": {"items": [{"id": "album", "images": None}]}},
            {"items": [{"name": "Track", "artists": [{"name": "Artist"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/1"}}]}]
        self.assertEqual(song.get_trending_song()["image_url"], "")

    @patch("src.song.request_json")
    def test_itunes_largest_artwork_stays_with_its_track(self, fetch):
        fetch.return_value = {"feed": {"updated": {"label": "2026-11-03"}, "entry": [
            {"im:name": {"label": "Song A"}, "im:artist": {"label": "Artist A"},
             "id": {"label": "https://music.apple.com/us/album/a"}, "im:image": [
                 {"label": "https://example.org/small.jpg", "attributes": {"height": "55"}},
                 {"label": "https://example.org/a.jpg", "attributes": {"height": "170"}}]},
            {"im:name": {"label": "Song B"}, "im:artist": {"label": "Artist B"},
             "id": {"label": "https://music.apple.com/us/album/b"}, "im:image": None}]}}
        cards = song.get_apple_music_cards()
        self.assertEqual(len(cards), 2)
        self.assertIn("Song A", cards[0]["text"])
        self.assertEqual(cards[0]["image_url"], "https://example.org/a.jpg")
        self.assertIn("Song B", cards[1]["text"])
        self.assertEqual(cards[1]["image_url"], "")
        self.assertIn("2026-11-03", cards[1]["text"])


@patch.object(config, "TELEGRAM_USER_ID", "123")
@patch.object(config, "TELEGRAM_CHANNEL_ID", "")
class PhotoDeliveryTests(unittest.TestCase):
    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_photo_payload_uses_utf16_entities(self, post):
        telegram_sender.send_photo("https://example.org/photo.jpg", "🎵 **آهنگ**")
        self.assertEqual(post.call_args.args[0], "sendPhoto")
        payload = json.loads(post.call_args.args[1])
        self.assertEqual(payload["photo"], "https://example.org/photo.jpg")
        self.assertEqual(payload["caption"], "🎵 آهنگ")
        self.assertEqual(payload["caption_entities"], [{"type": "bold", "offset": 3, "length": 4}])
        self.assertNotIn("parse_mode", payload)

    @patch("src.telegram_sender._post")
    def test_invalid_photo_and_overlong_caption_rejected_before_delivery(self, post):
        for url, caption in (("javascript:alert(1)", "text"), ("https://example.org/p.jpg", "🎵" * 513)):
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.send_photo(url, caption)
        post.assert_not_called()

    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_overflow_keeps_all_text_and_formatting_without_duplicate_photo(self, post):
        text = "🎵 **" + "خبر🎉" * 700 + "**\nhttps://example.org/source"
        telegram_sender.send_illustrated_message(text, "https://example.org/photo.jpg")
        calls = post.call_args_list
        self.assertEqual([call.args[0] for call in calls].count("sendPhoto"), 1)
        payloads = [json.loads(call.args[1]) for call in calls]
        self.assertEqual("".join(p.get("caption", p.get("text", "")) for p in payloads), formatting.render(text)[0])
        self.assertLessEqual(formatting.utf16_length(payloads[0]["caption"]), 1024)
        for payload in payloads:
            value = payload.get("caption", payload.get("text", ""))
            entities = payload.get("caption_entities", payload.get("entities", []))
            self.assertTrue(entities)
            for entity in entities:
                self.assertLessEqual(entity["offset"] + entity["length"], formatting.utf16_length(value))

    @patch("src.telegram_sender._post")
    def test_rejected_photo_falls_back_to_full_text(self, post):
        post.side_effect = [telegram_sender.TelegramError("photo rejected"), {"ok": True}]
        text = "**تیتر**\nخبر با منبع\nhttps://example.org/source"
        telegram_sender.send_illustrated_message(text, "https://example.org/broken.jpg")
        self.assertEqual([call.args[0] for call in post.call_args_list], ["sendPhoto", "sendMessage"])
        payload = json.loads(post.call_args.args[1])
        self.assertEqual(payload["text"], formatting.render(text)[0])

    @patch("src.telegram_sender._post")
    def test_continuation_failure_does_not_replay_delivered_photo(self, post):
        post.side_effect = [{"ok": True}, telegram_sender.TelegramError("text failed")]
        with self.assertRaises(telegram_sender.TelegramError):
            telegram_sender.send_illustrated_message("متن" * 700, "https://example.org/photo.jpg")
        self.assertEqual([call.args[0] for call in post.call_args_list], ["sendPhoto", "sendMessage"])

    @patch("src.telegram_sender._post", return_value={"ok": True})
    def test_absent_image_uses_plain_message(self, post):
        telegram_sender.send_illustrated_message("**تیتر**", None)
        self.assertEqual(post.call_args.args[0], "sendMessage")


class ReportImageTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, result in (("weather.get_mashhad_weather", None), ("news.get_diverse_news", {"جهان": [{}]}),
                             ("sports.get_sports_results", {}), ("writer.generate_daily_insight", "یادگیری"),
                             ("writer.prepare_news_cards", [{"text": "خبر", "image_url": "https://example.org/news.jpg"}]),
                             ("song.get_trending_song", {"title": "Track", "artist": "Artist",
                                                       "link": "https://open.spotify.com/track/1",
                                                       "image_url": "https://example.org/cover.jpg"})):
            self.stack.enter_context(patch(f"src.main.{name}", return_value=result))
        self.send = self.stack.enter_context(patch("src.main.telegram_sender.send_illustrated_message"))
        self.stack.enter_context(patch("src.main.song.get_new_music_cards", return_value=[]))
        self.download = self.stack.enter_context(patch("src.main.song.download_audio"))
        self.stack.enter_context(patch.object(config, "ENABLE_AUDIO_DOWNLOAD", False))
        self.stack.enter_context(patch.object(config, "TELEGRAM_BOT_TOKEN", "test"))
        self.stack.enter_context(patch.object(config, "TELEGRAM_USER_ID", "123"))

    def test_news_and_spotify_covers_reach_delivery(self):
        self.assertEqual(main.run(), 0)
        delivered = self.send.call_args_list
        self.assertTrue(any(call.args == ("خبر", "https://example.org/news.jpg") for call in delivered))
        self.assertTrue(any("Track" in call.args[0] and call.args[1] == "https://example.org/cover.jpg" for call in delivered))

    def test_preview_lists_images_without_sending_or_downloading(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.run(dry_run=True), 0)
        self.assertIn("https://example.org/news.jpg", output.getvalue())
        self.assertIn("https://example.org/cover.jpg", output.getvalue())
        self.send.assert_not_called()
        self.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
