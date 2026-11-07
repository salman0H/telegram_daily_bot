from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from src import song
import test_images

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def entry(number, days=1, date=None):
    return {'im:name': {'label': f'Song {number}'}, 'im:artist': {'label': f'Artist {number}'},
            'id': {'label': f'https://music.apple.com/song/{number}'},
            'im:releaseDate': {'label': date if date is not None else (NOW-timedelta(days=days)).isoformat()},
            'im:image': [{'label': f'https://example.org/{number}.jpg', 'attributes': {'height': '170'}}]}


class NewMusicTests(unittest.TestCase):
    @patch('src.song.request_json')
    def test_five_distinct_dated_cards_with_matching_artwork(self, fetch):
        fetch.return_value = {'feed': {'entry': [entry(n) for n in range(10)]}}
        cards = song.get_new_music_cards(NOW)
        self.assertEqual(len(cards), 5)
        self.assertEqual(len({c['image_url'] for c in cards}), 5)
        for card in cards:
            number = card['image_url'].split('/')[-1].split('.')[0]
            self.assertIn(f'Song {number}', card['text'])
            self.assertIn(f'https://music.apple.com/song/{number}', card['text'])
            self.assertIn('2026/11/06', card['text'])

    @patch('src.song.request_json')
    def test_old_future_missing_dates_duplicates_and_already_featured_links_are_excluded(self, fetch):
        fetch.return_value = {'feed': {'entry': [entry(1), entry(1), entry(2, days=40), entry(3, days=-1),
                                                entry(4, date=''), entry(5, date='2026-11-06'), entry(6)]}}
        cards = song.get_new_music_cards(NOW, exclude_links={'https://music.apple.com/song/6'})
        self.assertEqual(len(cards), 1)
        self.assertIn('Song 1', cards[0]['text'])

    @patch('src.song.request_json')
    def test_stable_pool_rotates_daily_without_repeating_yesterdays_group(self, fetch):
        fetch.return_value = {'feed': {'entry': [entry(n) for n in range(20)]}}
        today = song.get_new_music_cards(NOW)
        self.assertEqual(today, song.get_new_music_cards(NOW))
        tomorrow = song.get_new_music_cards(NOW + timedelta(days=1))
        self.assertFalse({c['image_url'] for c in today} & {c['image_url'] for c in tomorrow})

    @patch('src.song.request_json', side_effect=TimeoutError)
    def test_source_failure_returns_no_invented_releases(self, fetch):
        self.assertEqual(song.get_new_music_cards(NOW), [])


class NewMusicReportTests(unittest.TestCase):
    setUp = test_images.ReportImageTests.setUp
    def test_additional_new_cards_reach_report_without_replacing_spotify(self):
        from src import main
        with patch('src.main.song.get_new_music_cards', return_value=[
                {'text': f'Fresh {n}', 'image_url': f'https://example.org/fresh{n}.jpg'} for n in range(5)]) as fresh:
            report, track = main.build_report()
        self.assertEqual(track['title'], 'Track')
        self.assertEqual(sum(c['text'].startswith('Fresh ') for c in report['messages']), 5)
        self.assertTrue(any('Track' in c['text'] for c in report['messages']))
        self.assertIn('https://open.spotify.com/track/1', fresh.call_args.kwargs['exclude_links'])
