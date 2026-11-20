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
                                     ('BUTTON_URL_INVALID','telegram_miniapp_button_rejected'),
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

    def test_downloads_telegram_receipt_from_fixed_origin_without_redirects(self):
        token='123456:fixture-token-for-unit-tests'
        with patch('portal_reseller_bot.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            client.post.return_value=self.response({'file_path':'photos/file_1.jpg','file_size':5})
            response=client.get.return_value.__enter__.return_value
            response.status_code=200
            response.headers={}
            response.iter_content.return_value=iter([b'test',b'!'])
            self.assertEqual(TelegramTransport().download_file(token,'file-id'),b'test!')
            self.assertEqual(client.post.call_args.args,('https://api.telegram.org/bot'+token+'/getFile',))
            self.assertEqual(client.get.call_args.args,('https://api.telegram.org/file/bot'+token+'/photos/file_1.jpg',))
            self.assertFalse(client.get.call_args.kwargs['allow_redirects'])
            self.assertTrue(client.get.call_args.kwargs['stream'])
            self.assertFalse(client.trust_env)

    def test_download_rejects_external_paths_and_stream_caps(self):
        token='123456:fixture-token-for-unit-tests'
        with patch('portal_reseller_bot.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            transport=TelegramTransport()
            for path in ('https://example.test/file.jpg','photos/../file.jpg','photos/%2e%2e/file.jpg'):
                with self.subTest(path=path):
                    client.post.return_value=self.response({'file_path':path})
                    with self.assertRaisesRegex(BotError,'^receipt_file_unavailable$'):
                        transport.download_file(token,'file-id')
            client.get.assert_not_called()
            client.post.return_value=self.response({'file_path':'photos/file.jpg'})
            response=client.get.return_value.__enter__.return_value
            response.status_code=200
            response.headers={}
            response.iter_content.return_value=iter([b'abc',b'def'])
            with self.assertRaisesRegex(BotError,'^receipt_file_too_large$'):
                transport.download_file(token,'file-id',max_bytes=5)

if __name__=='__main__':unittest.main()
