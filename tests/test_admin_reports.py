from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from src import config, member_bot, subscribers, telegram_sender as telegram

class AdminReportsTests(unittest.TestCase):
    @patch.object(config, 'TELEGRAM_USER_ID', '123')
    @patch.object(config, 'TELEGRAM_CHANNEL_ID', '@public_channel')
    @patch('src.member_bot.telegram.call_api', return_value={'status': 'member'})
    @patch('src.member_bot.telegram.send_illustrated_message')
    @patch('src.member_bot.telegram.send_message')
    def test_success_notice_is_private_and_contains_id_and_username(self, send, cards, api):
        targets=[]
        send.side_effect=lambda text, **kwargs: targets.append((telegram._resolve_target(), text))
        report={'version':1,'generated_at':datetime.now(timezone.utc).isoformat(), 'messages':[{'text':'Card','image_url':''}]}
        bot=member_bot.MemberBot(report, subscribers=subscribers.Subscribers(), admin_reports=True)
        bot.handle_update({'message':{'chat':{'id':42,'type':'private'},'from':{'id':42,'username':'someone'},'text':'/start'}})
        notices=[text for target,text in targets if target=='123']
        self.assertEqual(len(notices),1)
        self.assertIn('ID: 42',notices[0])
        self.assertIn('@someone',notices[0])
        self.assertIn('گزارش کامل ارسال شد',notices[0])
        self.assertFalse(any(target=='@public_channel' for target,text in targets))
        self.assertEqual(bot.subscribers.users['42']['username'],'someone')

    @patch.object(config, 'TELEGRAM_USER_ID', '@channel')
    @patch('src.member_bot.telegram.send_message')
    def test_channel_or_missing_admin_never_receives_private_details(self, send):
        bot=member_bot.MemberBot(admin_reports=True)
        bot._notify_admin(42,'status')
        send.assert_not_called()

    @patch.object(config, 'TELEGRAM_USER_ID', '123')
    @patch('src.member_bot.telegram.send_message', side_effect=telegram.TelegramError('failed'))
    def test_admin_delivery_failure_does_not_fail_user_processing(self, send):
        bot=member_bot.MemberBot(admin_reports=True)
        bot._notify_admin(42,'status')
