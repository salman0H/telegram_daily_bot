from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from src import nasa, config, news, report_store

class NasaTests(unittest.TestCase):
    @patch.object(config, 'ENABLE_NASA', True)
    @patch('src.nasa.writer._call_groq', return_value=None)
    @patch('src.nasa.request_json')
    def test_original_and_source_survive_snapshot(self, fetch, model):
        fetch.return_value = {'date': datetime.now(timezone.utc).date().isoformat(), 'title': 'Space', 'explanation': 'Source explanation', 'media_type': 'image', 'url': 'https://example.org/small.jpg', 'hdurl': 'https://example.org/original.jpg'}
        card = nasa.get_card()
        report = report_store.validate_report({'version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(), 'messages': [card]})
        self.assertEqual(report['messages'][0]['document_url'], 'https://example.org/original.jpg')
        self.assertIn('زمان ثبت عکس نیست', card['text'])
        self.assertIn('apod.nasa.gov', card['source_url'])

    @patch('src.nasa.request_json')
    def test_disabled_does_not_fetch(self, fetch):
        with patch.object(config, 'ENABLE_NASA', False):
            self.assertIsNone(nasa.get_card())
        fetch.assert_not_called()

    def test_interest_boundaries(self):
        with patch.object(config, 'NEWS_KEYWORDS', ['iran', 'nvidia']):
            self.assertEqual(news.interest_score('Iran and NVIDIA'), 2)
            self.assertEqual(news.interest_score('spiraling'), 0)
