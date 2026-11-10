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

class FallbackTests(unittest.TestCase):
    @patch('src.nasa.request_json', side_effect=TimeoutError)
    @patch('src.nasa.writer._call_groq', return_value=None)
    @patch('src.nasa.request_bytes')
    def test_api_failure_uses_dated_official_article_and_original(self, pages, model, api):
        day = datetime.now(timezone.utc).date()
        source = f'https://science.nasa.gov/image-article/apod-{day.year}-{day.strftime("%B").lower()}-{day.day}-space/'
        original = 'https://assets.science.nasa.gov/content/dam/science/space.jpg'
        pages.side_effect = [f'<a href="{source}">Today</a>'.encode(),
            f'<meta property="og:title" content="APOD: {day.strftime("%Y %B")} {day.day} - Space - NASA Science"><a href="{original}"><img></a><strong>Explanation:</strong>Actual source text.</p><th>Credit &amp; Copyright</th><td>Photographer</td>'.encode()]
        card = nasa.get_card()
        self.assertEqual(card['document_url'], original)
        self.assertEqual(card['source_url'], source)
        self.assertIn('Photographer', card['text'])

    @patch('src.nasa.request_json', side_effect=TimeoutError)
    @patch('src.nasa.request_bytes', return_value=b'<html>Missing article</html>')
    def test_broken_fallback_is_omitted_without_guessing_an_image(self, pages, api):
        self.assertIsNone(nasa.get_card())
