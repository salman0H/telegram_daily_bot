"""Official HN schema, bounded collection, truthful metadata, and report integration."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from src import config, hacker_news, main, report_store
import test_images

NOW = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)


def item(number=1, **changes):
    return {"id": number, "type": "story", "title": f"Story {number}",
            "url": f"https://example.org/{number}", "time": int(NOW.timestamp()),
            "score": 42, "descendants": 7, **changes}


@patch.object(config, "ENABLE_HACKER_NEWS", True)
@patch.object(config, "HACKER_NEWS_PER_DAY", 3)
@patch.object(config, "NEWS_MAX_AGE_HOURS", 36)
class CollectionTests(unittest.TestCase):
    @patch("src.hacker_news.request_json")
    def test_preserves_ranking_and_source_metadata(self, fetch):
        def respond(url, **kwargs):
            self.assertEqual(kwargs, {"timeout": 5, "attempts": 1})
            if "topstories" in url:
                return [5, 2, 7]
            number = int(url.rsplit("/", 1)[1].split(".")[0])
            return item(number, title=f"Story &amp; {number}")
        fetch.side_effect = respond
        stories = hacker_news.get_stories(NOW)
        self.assertEqual([s["title"] for s in stories], ["Story & 5", "Story & 2", "Story & 7"])
        self.assertEqual(stories[0]["discussion_url"], "https://news.ycombinator.com/item?id=5")
        self.assertEqual(stories[0]["score"], 42)
        self.assertEqual(stories[0]["image_url"], "")

    @patch("src.hacker_news.request_json")
    def test_deleted_dead_nonstory_mismatched_and_invalid_records_skipped(self, fetch):
        invalid = [None, [], item(deleted=True), item(dead=True), item(type="job"),
                   item(id=2), item(id=True), item(id=1.0), item(time=True),
                   item(time="bad"), item(time=10**100), item(title=[]), item(title=""),
                   item(url="javascript:alert(1)"), item(url={}),
                   item(time=int((NOW - timedelta(hours=37)).timestamp())),
                   item(time=int((NOW + timedelta(minutes=6)).timestamp()))]
        for record in invalid:
            with self.subTest(record=record):
                fetch.return_value = record
                self.assertIsNone(hacker_news._fetch_story(1, NOW))

    @patch("src.hacker_news.request_json", return_value=item(url=None, text="<p>A source &amp; its text</p>"))
    def test_selfpost_links_to_discussion_and_removes_html(self, fetch):
        story = hacker_news._fetch_story(1, NOW)
        self.assertEqual(story["link"], story["discussion_url"])
        self.assertEqual(story["summary"], "A source & its text")

    @patch("src.hacker_news.request_json", return_value=item(score=True, descendants=-1))
    def test_invalid_metrics_are_not_fabricated(self, fetch):
        story = hacker_news._fetch_story(1, NOW)
        self.assertIsNone(story["score"])
        self.assertIsNone(story["comments"])

    @patch("src.hacker_news.request_json")
    def test_duplicate_links_titles_and_ids_are_removed(self, fetch):
        values = {1: item(1), 2: item(2, url="https://example.org/1#anchor"),
                  3: item(3, title="STORY 1"), 4: item(4)}
        fetch.side_effect = lambda url, **kwargs: [1, 1, 2, 3, 4] if "topstories" in url else values[int(url.rsplit("/", 1)[1].split(".")[0])]
        stories = hacker_news.get_stories(NOW)
        self.assertEqual([s["link"] for s in stories], ["https://example.org/1", "https://example.org/4"])

    @patch("src.hacker_news._fetch_story", return_value=None)
    @patch("src.hacker_news.request_json", return_value=[True, -1, 0, "bad", *range(1, 501)])
    def test_candidate_requests_are_bounded(self, ranking, story):
        self.assertEqual(hacker_news.get_stories(NOW), [])
        self.assertEqual(story.call_count, 25)

    @patch("src.hacker_news.request_json", side_effect=TimeoutError)
    def test_failure_omits_only_this_section(self, fetch):
        self.assertEqual(hacker_news.get_stories(NOW), [])

    @patch("src.hacker_news.request_json")
    def test_toggle_makes_no_requests(self, fetch):
        with patch.object(config, "ENABLE_HACKER_NEWS", False):
            self.assertEqual(hacker_news.get_stories(NOW), [])
        fetch.assert_not_called()


class CardTests(unittest.TestCase):
    @patch("src.hacker_news.request_json", return_value=item())
    @patch("src.writer._call_groq", return_value=[{"id": "n1", "title": "تیتر فارسی", "summary": "خلاصهٔ عنوان"}])
    def test_persian_card_preserves_links_and_labels_title_only_basis(self, generate, fetch):
        story = hacker_news._fetch_story(1, NOW)
        card = hacker_news.prepare_cards([story])[0]
        self.assertIn("تیتر فارسی", card["text"])
        self.assertIn("متن مقاله دریافت نشده", card["text"])
        self.assertIn(story["link"], card["text"])
        self.assertIn(story["discussion_url"], card["text"])
        self.assertIn("امتیاز: 42", card["text"])
        self.assertIn("نه لزوماً انتشار مقاله", card["text"])
        self.assertEqual(card["image_url"], "")

    @patch("src.hacker_news.request_json", return_value=item(score=None, descendants=None, url=None, text="<p>Post text</p>"))
    @patch("src.writer._call_groq", return_value=None)
    def test_fallback_keeps_original_without_inventing_metrics_or_duplicate_link(self, generate, fetch):
        story = hacker_news._fetch_story(1, NOW)
        card = hacker_news.prepare_cards([story])[0]
        self.assertIn("Story 1", card["text"])
        self.assertIn("Post text", card["text"])
        self.assertNotIn("امتیاز:", card["text"])
        self.assertEqual(card["text"].count(story["link"]), 1)


class ReportIntegrationTests(unittest.TestCase):
    setUp = test_images.ReportImageTests.setUp

    def test_hn_is_in_shared_report_alongside_existing_sections(self):
        story = {"link": "https://example.org/hn"}
        with patch("src.main.hacker_news.get_stories", return_value=[story]), patch(
                "src.main.hacker_news.prepare_cards", return_value=[{"text": "HN section", "image_url": ""}]):
            report, _ = main.build_report()
        self.assertIn("HN section", [card["text"] for card in report["messages"]])
        self.assertTrue(any(card["image_url"] for card in report["messages"]))
        self.assertEqual(report_store.validate_report(report)["messages"], report["messages"])

    def test_duplicate_of_existing_news_is_not_rendered_again(self):
        with patch("src.main.news.get_diverse_news", return_value={"BBC": [{"link": "https://example.org/shared"}]}), patch(
                "src.main.hacker_news.get_stories", return_value=[{"link": "https://example.org/shared"}]), patch(
                "src.main.hacker_news.prepare_cards") as prepare:
            main.build_report()
        prepare.assert_not_called()

    def test_enlarged_snapshot_limit_remains_bounded(self):
        report = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                  "messages": [{"text": "Card", "image_url": ""}] * 41}
        self.assertEqual(len(report_store.validate_report(report)["messages"]), 41)
        report["messages"] *= 2
        with self.assertRaises(ValueError):
            report_store.validate_report(report)
