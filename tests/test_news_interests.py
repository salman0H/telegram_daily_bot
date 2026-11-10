import unittest
from unittest.mock import patch
from src import config, hacker_news
from test_hacker_news import NOW, item

class InterestTests(unittest.TestCase):
    def test_file_is_default_and_override_takes_precedence(self):
        with patch.dict('os.environ', {'NEWS_KEYWORDS': ''}):
            self.assertIn('nvidia', [word.casefold() for word in config._news_keywords()])
        with patch.dict('os.environ', {'NEWS_KEYWORDS': 'custom topic, another'}):
            self.assertEqual(config._news_keywords(), ['custom topic', 'another'])

    @patch.object(config, 'ENABLE_HACKER_NEWS', True)
    @patch.object(config, 'HACKER_NEWS_PER_DAY', 1)
    @patch.object(config, 'NEWS_KEYWORDS', ['nvidia'])
    @patch('src.hacker_news.request_json', return_value=[1, 2])
    @patch('src.hacker_news._fetch_story')
    def test_later_matching_story_beats_earlier_nonmatching_story(self, fetch, ranking):
        def story(number, now):
            return {'title': 'NVIDIA chips' if number == 2 else 'Other topic',
                    'summary': '', 'link': f'https://example.org/{number}'}
        fetch.side_effect = story
        selected = hacker_news.get_stories(NOW)
        self.assertEqual(selected[0]['title'], 'NVIDIA chips')
