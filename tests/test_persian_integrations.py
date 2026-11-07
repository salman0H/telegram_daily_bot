from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from src import config, main, persian, persian_content, writer
import test_images

NOW = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)


class PresentationTests(unittest.TestCase):
    def test_known_jalali_boundary_and_tehran_midnight(self):
        with patch.object(config, 'LOCAL_TZ', __import__('zoneinfo').ZoneInfo('Asia/Tehran')):
            self.assertIn('۱۴۰۵/۰۸/۱۶', persian.local_stamp(NOW))
            self.assertIn('04:00', persian.local_stamp(NOW))
            self.assertIn('۱۴۰۴/۰۱/۰۱', persian.local_stamp(datetime(2025, 3, 20, 22, tzinfo=timezone.utc)))

    def test_normalizes_prose_without_converting_latin_names_or_numbers(self):
        self.assertEqual(writer.plain_field('كتاب فارسي Python 3.14'), 'کتاب فارسی Python 3.14')


class CalendarTests(unittest.TestCase):
    @patch.object(config, 'ENABLE_PERSIAN_CALENDAR', True)
    @patch('src.persian_content.request_json')
    def test_calendar_validated_status_deduplication_and_source(self, fetch):
        fetch.return_value = {'is_holiday': False, 'events': [{'description': 'روز كتاب'}, {'description': 'روز کتاب'}]}
        card = persian_content.get_calendar_card(NOW)
        self.assertEqual(card['text'].count('روز کتاب'), 1)
        self.assertIn('تعطیلی در منبع ثبت نشده', card['text'])
        self.assertIn('https://holidayapi.ir', card['text'])
        self.assertTrue(fetch.call_args.args[0].endswith('/2026/11/7'))

    @patch.object(config, 'ENABLE_PERSIAN_CALENDAR', True)
    @patch('src.persian_content.request_json', return_value={'is_holiday': 'false', 'events': []})
    def test_invalid_boolean_is_not_published_as_holiday_fact(self, fetch):
        card = persian_content.get_calendar_card(NOW)
        self.assertIn('اطلاعات مناسبت و تعطیلی در دسترس نیست', card['text'])
        self.assertIn('۱۴۰۵', card['text'])
        self.assertNotIn('تعطیل طبق منبع', card['text'])

    @patch.object(config, 'ENABLE_PERSIAN_CALENDAR', True)
    @patch('src.persian_content.request_json', side_effect=TimeoutError)
    def test_timeout_preserves_offline_date(self, fetch):
        self.assertIn('۱۴۰۵/۰۸/۱۶', persian_content.get_calendar_card(NOW)['text'])

    @patch.object(config, 'ENABLE_PERSIAN_CALENDAR', False)
    @patch('src.persian_content.request_json')
    def test_disabled_makes_no_request(self, fetch):
        self.assertIsNone(persian_content.get_calendar_card(NOW))
        fetch.assert_not_called()


class PoetryTests(unittest.TestCase):
    def setUp(self):
        self.groq = patch("src.writer._call_groq", return_value=None)
        self.groq.start()
        self.addCleanup(self.groq.stop)

    def test_daily_selection_is_stable_and_changes_at_tehran_midnight(self):
        with patch.object(config, 'LOCAL_TZ', __import__('zoneinfo').ZoneInfo('Asia/Tehran')):
            before = datetime(2026, 11, 7, 20, 29, tzinfo=timezone.utc)
            after = before + timedelta(minutes=1)
            self.assertEqual(persian_content.daily_hafez_path(NOW), persian_content.daily_hafez_path(before))
            self.assertNotEqual(persian_content.daily_hafez_path(before), persian_content.daily_hafez_path(after))

    def test_daily_cycle_has_no_repeats_for_495_days(self):
        choices = {persian_content.daily_hafez_path(NOW + timedelta(days=day)) for day in range(495)}
        self.assertEqual(len(choices), 495)
        self.assertEqual({int(path.rsplit('sh', 1)[1]) for path in choices}, set(range(1, 496)))

    @patch.object(config, 'ENABLE_PERSIAN_POETRY', True)
    @patch('src.persian_content.request_json', side_effect=TimeoutError)
    def test_unavailable_fal_is_not_replaced_with_invented_poem(self, fetch):
        self.assertIsNone(persian_content.get_poetry_card(NOW))

    def poem(self):
        return {'fullTitle': 'حافظ » غزلیات » غزل اول', 'fullUrl': persian_content.daily_hafez_path(NOW),
                'plainText': 'مصراع نخست\r\nمصراع دوم\r\nمصراع سوم\r\nمصراع چهارم', 'poemSummary': 'FAKE AI INTERPRETATION'}

    @patch.object(config, 'ENABLE_PERSIAN_POETRY', True)
    @patch('src.persian_content.request_json')
    def test_full_daily_ghazal_and_fixed_author(self, fetch):
        fetch.return_value = {'poem': self.poem()}
        card = persian_content.get_poetry_card(NOW)
        self.assertIn('مصراع نخست\nمصراع دوم', card['text'])
        self.assertIn('مصراع سوم\nمصراع چهارم', card['text'])
        self.assertNotIn('INTERPRETATION', card['text'])
        self.assertIn('https://ganjoor.net' + persian_content.daily_hafez_path(NOW), card['text'])
        self.assertIn('/api/ganjoor/page?', fetch.call_args.args[0])
        self.assertIn('فال حافظ روزانه', card['text'])

    @patch.object(config, 'ENABLE_PERSIAN_POETRY', True)
    @patch('src.persian_content.request_json')
    def test_invalid_source_or_author_or_incomplete_verse_is_skipped(self, fetch):
        for change in ({'fullUrl': '//evil.test/hafez/a'}, {'fullTitle': 'Someone else'}, {'plainText': 'One line'}):
            fetch.return_value = {'poem': {**self.poem(), **change}}
            self.assertIsNone(persian_content.get_poetry_card(NOW))

    @patch.object(config, 'ENABLE_PERSIAN_POETRY', False)
    @patch('src.persian_content.request_json')
    def test_disabled_makes_no_request(self, fetch):
        self.assertIsNone(persian_content.get_poetry_card(NOW))
        fetch.assert_not_called()


# Reuse only fixture setup, without rediscovering inherited test cases.
class ReportTests(unittest.TestCase):
    setUp = test_images.ReportImageTests.setUp

    def test_calendar_and_poem_are_part_of_the_shared_snapshot(self):
        with patch('src.main.persian_content.get_calendar_card', return_value={'text': 'Calendar'}), \
             patch('src.main.persian_content.get_poetry_card', return_value={'text': 'Poem'}):
            snapshot, _ = main.build_report()
        self.assertEqual(snapshot['messages'][0]['text'], 'Calendar')
        self.assertTrue(any(card['text'] == 'Poem' for card in snapshot['messages']))
