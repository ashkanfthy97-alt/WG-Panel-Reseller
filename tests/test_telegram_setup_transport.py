"""Real shared transport, mocked HTTP: setup cannot contact Telegram in tests."""
import os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.append(str(Path(os.environ.get('OPERATIONS_SUPPORT_ROOT',ROOT))))
from portal_reseller_bot import BotError,TelegramTransport

class Transport(unittest.TestCase):
    def response(self,value):return SimpleNamespace(status_code=200,content=b'{}',json=lambda:{'ok':True,'result':value})
    def test_getchat_supported_with_bounded_setup_timeout(self):
        with patch('portal_reseller_bot.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            client.post.return_value=self.response({'id':12345,'type':'private'})
            value=TelegramTransport().call('123456:fixture-token-for-unit-tests','getChat',{'chat_id':12345},timeout=(3,5))
            self.assertEqual(value['id'],12345)
            self.assertEqual(client.post.call_args.kwargs['timeout'],(3,5))
            self.assertFalse(client.post.call_args.kwargs['allow_redirects'])
            self.assertFalse(client.trust_env)
    def test_existing_poll_and_send_timeout_contracts_preserved(self):
        with patch('portal_reseller_bot.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            client.post.return_value=self.response([])
            TelegramTransport().call('123456:fixture-token-for-unit-tests','getUpdates')
            self.assertEqual(client.post.call_args.kwargs['timeout'],(5,40))
            client.post.return_value=self.response({'id':1})
            TelegramTransport().call('123456:fixture-token-for-unit-tests','sendMessage',{'chat_id':12345,'text':'آزمایش'})
            self.assertEqual(client.post.call_args.kwargs['timeout'],(5,15))

    def test_edit_message_recovers_only_from_specific_telegram_errors(self):
        with patch('portal_reseller_bot.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            transport=TelegramTransport()
            token='123456:fixture-token-for-unit-tests'
            payload={'chat_id':12345,'message_id':71,'text':'آزمایش'}
            def reject(description):
                client.post.return_value=SimpleNamespace(status_code=400,content=b'{}',
                    json=lambda:{'ok':False,'error_code':400,'description':description})
            reject('Bad Request: message is not modified: same content')
            self.assertIs(transport.call(token,'editMessageText',payload),True)
            for description,code in [('message to edit not found','telegram_message_uneditable'),
                                     ('message text is empty','telegram_bad_request')]:
                with self.subTest(description=description):
                    reject('Bad Request: '+description)
                    with self.assertRaises(BotError) as error:
                        transport.call(token,'editMessageText',payload)
                    self.assertEqual(error.exception.code,code)
            reject('Bad Request: message to edit not found')
            with self.assertRaises(BotError) as error:
                transport.call(token,'sendMessage',{'chat_id':12345,'text':'آزمایش'})
            self.assertEqual(error.exception.code,'telegram_bad_request')

if __name__=='__main__':unittest.main()
