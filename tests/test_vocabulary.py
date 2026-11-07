from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src import config, vocabulary, writer, main
import test_images

NOW = datetime(2026, 11, 7, 10, tzinfo=timezone.utc)


class VocabularyTests(unittest.TestCase):
    def setUp(self):
        self.entry = vocabulary.load_entries()[0]

    def test_all_user_rows_preserved_with_unknown_metrics(self):
        entries = vocabulary.load_entries()
        self.assertEqual(len(entries), 474)
        self.assertEqual({e['id'] for e in entries}, set(range(1, 475)))
        self.assertEqual(self.entry['word'], 'plateau')
        self.assertEqual(self.entry['translation'], 'یک دوره توقف یا کندی پیشرفت')
        self.assertEqual(self.entry['results'][0]['synonyms'], ['level off', 'stagnation', 'flat period'])
        self.assertTrue(all(e['frequency'] is None and e['syllables'] is None for e in entries))

    def test_stable_daily_rotation_unique_and_changes_next_day(self):
        entries = vocabulary.load_entries()
        today = vocabulary.select_daily(entries, NOW)
        self.assertEqual(today, vocabulary.select_daily(entries, NOW + timedelta(hours=1)))
        self.assertEqual(len({e['word'].casefold() for e in today}), 5)
        self.assertFalse({e['id'] for e in today} & {e['id'] for e in vocabulary.select_daily(entries, NOW + timedelta(days=1))})

    def test_duplicate_word_spellings_do_not_repeat_within_day(self):
        rows = [self.entry, {**self.entry, 'id': 99, 'word': 'PLATEAU'}]
        self.assertEqual(len(vocabulary.select_daily(rows, NOW)), 1)

    @patch.object(config, 'ENABLE_VOCABULARY_DICTIONARY', True)
    @patch('src.vocabulary.request_json')
    def test_dictionary_enriches_pronunciation_and_matching_example_without_changing_personal_meaning(self, fetch):
        fetch.return_value = [{'word': 'plateau', 'phonetic': '/plæˈtəʊ/',
                              'phonetics': [{'audio': '//example.org/audio.mp3'}],
                              'meanings': [{'definitions': [{'definition': 'A period when progress stops and becomes slow.',
                                                            'example': 'Her progress reached a plateau.'}]}]}]
        original = deepcopy(self.entry)
        detail = vocabulary.dictionary_details(self.entry)
        self.assertEqual(self.entry, original)
        self.assertEqual(detail['audio'], 'https://example.org/audio.mp3')
        self.assertIn('reached a plateau', detail['example'])
        card = vocabulary.render_card(self.entry, 1, 5, detail)['text']
        for text in ('یک دوره توقف', 'English definition', 'Synonyms / Equivalents', 'noun / verb', '/plæˈtəʊ/', 'Her progress'):
            self.assertIn(text, card)

    @patch.object(config, 'ENABLE_VOCABULARY_DICTIONARY', True)
    @patch('src.vocabulary.request_json')
    def test_different_sense_example_is_not_attached(self, fetch):
        fetch.return_value = [{'word': 'plateau', 'meanings': [{'definitions': [
            {'definition': 'An elevated flat area of land.', 'example': 'We walked across the plateau.'}]}]}]
        self.assertNotIn('example', vocabulary.dictionary_details(self.entry))

    @patch.object(config, 'ENABLE_VOCABULARY_DICTIONARY', True)
    @patch('src.vocabulary.request_json')
    def test_phrases_are_not_sent_as_single_word_dictionary_queries(self, fetch):
        self.assertEqual(vocabulary.dictionary_details({**self.entry, 'word': 'make ends meet'}), {})
        fetch.assert_not_called()

    @patch.object(config, 'ENABLE_VOCABULARY', True)
    @patch.object(config, 'ENABLE_VOCABULARY_DICTIONARY', True)
    @patch('src.vocabulary.request_json', side_effect=TimeoutError)
    def test_network_failure_preserves_all_offline_fields(self, fetch):
        with patch('src.vocabulary.select_daily', return_value=[self.entry]):
            cards = vocabulary.get_vocabulary_cards(NOW)
        self.assertEqual(len(cards), 1)
        self.assertIn(self.entry['results'][0]['definition'], cards[0]['text'])
        self.assertIn('level off', cards[0]['text'])
        self.assertNotIn('5.83', cards[0]['text'])

    def test_missing_source_fields_are_explicit_not_html_placeholders(self):
        entry = next(e for e in vocabulary.load_entries() if e['id'] == 112)
        text = vocabulary.render_card(entry, 1, 1)['text']
        self.assertIn('در فهرست', text)
        self.assertNotIn('<br>', text)
        self.assertIn('Jersey', text)

    @patch.object(config, 'ENABLE_VOCABULARY', False)
    @patch('src.vocabulary.load_entries')
    @patch('src.vocabulary.request_json')
    def test_disabled_does_not_read_or_fetch(self, fetch, load):
        self.assertEqual(vocabulary.get_vocabulary_cards(), [])
        load.assert_not_called()
        fetch.assert_not_called()

    def test_optional_fields_can_be_added_without_fabricating_missing_values(self):
        entry = deepcopy(self.entry)
        entry['pronunciation'] = {'all': '/sample/'}
        entry['results'][0]['examples'] = ['An example supplied by the owner.']
        entry['syllables'] = {'count': 2, 'list': ['pla', 'teau']}
        entry['frequency'] = 5.83
        text = vocabulary.render_card(entry, 1, 1)['text']
        for value in ('/sample/', 'An example supplied by the owner.', 'pla · teau', '5.83'):
            self.assertIn(value, text)

    def test_malformed_bank_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text(json.dumps({'version': 1, 'entries': [{'word': 'hello'}]}))
            with self.assertRaises(ValueError):
                vocabulary.load_entries(path)


class HafezExplanationTests(unittest.TestCase):
    @patch('src.writer._call_groq')
    def test_source_explanation_is_attributed_and_does_not_require_model(self, model):
        summary = 'هوش مصنوعی: این غزل دربارهٔ امید و گذشت زمان است و شاعر تصویرهای گوناگون عشق را برای بیان احساس خود به کار می‌گیرد.'
        text, label = writer.explain_hafez('حافظ', 'شعر', summary)
        self.assertIn('دربارهٔ امید', text)
        self.assertIn('گنجور', label)
        self.assertIn('هوش مصنوعی', label)
        self.assertFalse(text.startswith('هوش مصنوعی:'))
        model.assert_not_called()

    @patch('src.writer._call_groq', return_value=[{'id': 'fal', 'summary': 'شاعر در این غزل از امید و گذشت زمان سخن می‌گوید و تصویرهای ادبی را به هم پیوند می‌دهد.'}])
    def test_model_fallback_is_labeled_and_receives_original_text(self, model):
        text, label = writer.explain_hafez('حافظ', 'متن اصلی همین غزل')
        self.assertIn('شاعر', text)
        self.assertIn('هوش مصنوعی', label)
        self.assertNotIn('گنجور', label)
        self.assertIn('متن اصلی همین غزل', model.call_args.args[0])

    @patch('src.writer._call_groq', return_value=None)
    def test_no_explanation_is_not_presented_as_an_invented_reading(self, model):
        text, _ = writer.explain_hafez('حافظ', 'شعر')
        self.assertIn('در دسترس نیست', text)


class VocabularyReportTests(unittest.TestCase):
    setUp = test_images.ReportImageTests.setUp

    def test_cards_are_in_shared_snapshot_with_existing_sections(self):
        with patch('src.main.vocabulary.get_vocabulary_cards', return_value=[{'text': 'Vocabulary', 'image_url': ''}]):
            report, _ = main.build_report()
        self.assertTrue(any(card['text'] == 'Vocabulary' for card in report['messages']))
        self.assertTrue(any('Track' in card['text'] for card in report['messages']))
