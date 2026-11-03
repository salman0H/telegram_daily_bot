"""Larger CDN renditions and handling arrivals during an active listener window."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch
from src import config, media, member_bot, song, telegram_sender as telegram

BBC_SMALL = 'https://ichef.bbci.co.uk/news/240/cpsprodpb/story.jpg'
BBC_LARGE = 'https://ichef.bbci.co.uk/news/1024/cpsprodpb/story.jpg'


class ImageVariantTests(unittest.TestCase):
    def test_bbc_legacy_and_modern_paths_keep_same_asset(self):
        self.assertEqual(media.photo_variants(BBC_SMALL), [BBC_LARGE, BBC_SMALL])
        small = 'https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/a.jpg?quality=90'
        self.assertEqual(media.photo_variants(small)[0], 'https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/a.jpg?quality=90')

    def test_itunes_cover_rendition_preserves_source_and_encoding(self):
        small = 'https://is1-ssl.mzstatic.com/image/thumb/Music/a.jpg/170x170bb-85.png'
        self.assertEqual(media.photo_variants(small), [
            'https://is1-ssl.mzstatic.com/image/thumb/Music/a.jpg/1000x1000bb-85.png', small])

    def test_large_and_unknown_images_are_not_downscaled_or_rewritten(self):
        for image in ('https://ichef.bbci.co.uk/news/1920/cpsprodpb/a.jpg',
                      'https://is1-ssl.mzstatic.com/image/thumb/a/1200x1200bb.jpg',
                      'https://example.org/170x170bb.jpg',
                      'https://ichef.bbci.co.uk.evil.test/news/240/a.jpg',
                      'https://fake.mzstatic.com.evil.test/170x170bb.jpg',
                      'https://i.scdn.co/image/opaque-asset-id'):
            self.assertEqual(media.photo_variants(image), [image])

    def test_invalid_urls_and_missing_dimensions_are_safe(self):
        for value in (None, '', 42, 'javascript:alert(1)', 'https://user:pass@example.org/a.jpg'):
            self.assertEqual(media.photo_variants(value), [])
        for item in (None, {}, {'width': None}, {'width': 'bad', 'height': 640}):
            self.assertEqual(media.image_area(item), 0)

    @patch.object(config, 'SPOTIFY_CLIENT_ID', 'test')
    @patch.object(config, 'SPOTIFY_CLIENT_SECRET', 'test')
    @patch('src.song.request_json')
    def test_spotify_selects_largest_declared_cover_even_if_unsorted(self, fetch):
        fetch.side_effect = [{'access_token': 'token'}, {'albums': {'items': [{'id': 'album', 'images': [
            {'url': 'https://example.org/small.jpg', 'width': 64, 'height': 64},
            {'url': 'https://example.org/large.jpg', 'width': 640, 'height': 640},
            {'url': 'https://example.org/medium.jpg', 'width': 300, 'height': 300}]}]}},
            {'items': [{'name': 'Song', 'artists': [{'name': 'Artist'}],
                        'external_urls': {'spotify': 'https://open.spotify.com/track/1'}}]}]
        self.assertEqual(song.get_trending_song()['image_url'], 'https://example.org/large.jpg')


@patch.object(config, 'TELEGRAM_CHANNEL_ID', '')
@patch.object(config, 'TELEGRAM_USER_ID', '42')
class PhotoQualityDeliveryTests(unittest.TestCase):
    @patch('src.telegram_sender._post', return_value={'ok': True})
    def test_cached_thumbnail_is_upgraded_at_delivery_time(self, post):
        telegram.send_illustrated_message('**Headline**', BBC_SMALL)
        payload = json.loads(post.call_args.args[1])
        self.assertEqual(payload['photo'], BBC_LARGE)
        self.assertEqual(payload['caption'], 'Headline')
        self.assertEqual(post.call_count, 1)

    @patch('src.telegram_sender._post')
    def test_explicit_rejection_tries_original_image_before_text(self, post):
        post.side_effect = [telegram.TelegramError('rejected', status_code=400), {'ok': True}]
        telegram.send_illustrated_message('Headline', BBC_SMALL)
        payloads = [json.loads(call.args[1]) for call in post.call_args_list]
        self.assertEqual([payload['photo'] for payload in payloads], [BBC_LARGE, BBC_SMALL])
        self.assertTrue(all(call.args[0] == 'sendPhoto' for call in post.call_args_list))

    @patch('src.telegram_sender._post')
    def test_all_rejected_renditions_fall_back_to_full_text(self, post):
        post.side_effect = [telegram.TelegramError('large rejected', status_code=400),
                            telegram.TelegramError('original rejected', status_code=400), {'ok': True}]
        telegram.send_illustrated_message('**Headline**\nSource', BBC_SMALL)
        self.assertEqual([call.args[0] for call in post.call_args_list], ['sendPhoto', 'sendPhoto', 'sendMessage'])
        self.assertEqual(json.loads(post.call_args.args[1])['text'], 'Headline\nSource')

    @patch('src.telegram_sender._post')
    def test_uncertain_network_failure_does_not_retry_another_photo(self, post):
        post.side_effect = [telegram.TelegramError('delivery uncertain'), {'ok': True}]
        telegram.send_illustrated_message('Caption', BBC_SMALL)
        self.assertEqual([call.args[0] for call in post.call_args_list], ['sendPhoto', 'sendMessage'])

    @patch('src.telegram_sender._post', side_effect=telegram.RecipientUnavailableError('blocked', chat_id=42))
    def test_blocked_recipient_does_not_retry_original_rendition(self, post):
        with self.assertRaises(telegram.RecipientUnavailableError):
            telegram.send_illustrated_message('Caption', BBC_SMALL)
        self.assertEqual(post.call_count, 1)


@patch.object(config, 'BOT_ENABLED', True)
@patch.object(config, 'MEMBERSHIP_CHANNEL_ID', '@sentencedIntoMusic')
@patch.object(config, 'CHANNEL_JOIN_URL', '')
class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.clock = lambda: self.elapsed
        report = {'version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
                  'messages': [{'text': 'Report', 'image_url': ''}]}
        self.bot = member_bot.MemberBot(report, clock=self.clock)
        self.bot.check_setup = Mock()

    @patch('src.member_bot.time.sleep')
    @patch('src.member_bot.telegram.send_message')
    @patch('src.member_bot.telegram.send_illustrated_message')
    @patch('src.member_bot.telegram.call_api')
    def test_request_arriving_after_initial_empty_poll_is_processed(self, api, send_card, send_text, sleep):
        self.polls = 0
        def call(method, payload, **kwargs):
            if method == 'getChatMember':
                return {'status': 'member'}
            if payload['limit'] == 1:
                return []
            self.polls += 1
            self.elapsed += payload['timeout']
            self.assertGreater(kwargs['timeout'], payload['timeout'])
            if self.polls == 2:
                return [{'update_id': 10, 'message': {'chat': {'id': 42, 'type': 'private'},
                                                     'from': {'id': 42}, 'text': '/report'}}]
            return []
        api.side_effect = call
        self.bot.process_pending(listen_seconds=90)
        self.assertEqual(self.polls, 3)
        send_card.assert_called_once_with('Report', '')
        self.assertEqual(self.bot.membership_checks, 1)
        self.assertEqual(self.bot.updates_processed, 1)
        self.assertTrue(any(call.args[0] == 'getUpdates' and call.args[1].get('offset') == 11 for call in api.call_args_list))

    @patch('src.member_bot.time.sleep')
    @patch('src.member_bot.telegram.call_api')
    def test_quiet_listener_stops_at_its_time_budget(self, api, sleep):
        def call(method, payload, **kwargs):
            self.elapsed += payload['timeout']
            return []
        api.side_effect = call
        self.bot.process_pending(listen_seconds=90)
        self.assertEqual(self.elapsed, 60)
        self.assertEqual(api.call_count, 3)
        self.assertEqual(self.bot.membership_checks, 0)

    @patch('src.member_bot.telegram.call_api', return_value=[])
    def test_one_batch_mode_still_exits_immediately(self, api):
        self.bot.process_pending()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1]['timeout'], 0)

    @patch('src.member_bot.telegram.call_api')
    def test_invalid_listen_budget_does_not_contact_telegram(self, api):
        for seconds in (-1, 241):
            with self.assertRaises(ValueError):
                self.bot.process_pending(listen_seconds=seconds)
        api.assert_not_called()
