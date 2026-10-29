"""Shared native Telegram card formatting without network or Telegram clients."""
from html.parser import HTMLParser
import re
import unittest


class BalancedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.text=[]

    def handle_starttag(self,tag,attrs):
        if tag not in {'br'}:
            self.stack.append(tag)

    def handle_endtag(self,tag):
        if not self.stack or self.stack.pop()!=tag:
            raise AssertionError('Unbalanced generated HTML')

    def handle_data(self,data):
        self.text.append(data)


class PeerCardChecks(unittest.TestCase):
    def test_filename_uses_native_name_sanitization_and_safe_fallback(self):
        from telegram_peer_cards import safe_peer_name
        self.assertEqual(safe_peer_name(12,'Mamadreza'),'Mamadreza')
        for name in ('../../../../','<img src=x>','نام فارسی','a\\b\x00c',' . '):
            with self.subTest(name=name):
                value=safe_peer_name(12,name)
                self.assertTrue(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*',value))
                self.assertNotIn('..',value)
                self.assertNotIn('/',value)
                self.assertNotIn('\\',value)

    def test_short_caption_has_full_escaped_config_and_short_link(self):
        from telegram_peer_cards import peer_card_caption
        name='A <script>& customer'
        config='[Interface]\nPrivateKey = value<&>\n[Peer]\nAllowedIPs = 0.0.0.0/0'
        url='https://panel.example/u/token?a=1&b=2'
        for qr in (False,True):
            with self.subTest(qr=qr):
                value=peer_card_caption(name,config,url,qr=qr)
                self.assertLessEqual(len(value),1024)
                self.assertIn('<pre><code>',value)
                self.assertNotIn('<script>',value)
                self.assertIn('&lt;',value)
                parser=BalancedHTML()
                parser.feed(value)
                self.assertFalse(parser.stack)
                text=''.join(parser.text)
                self.assertIn(config,text)
                self.assertIn(url,text)

    def test_long_escaped_content_stays_bounded_with_balanced_markup(self):
        from telegram_peer_cards import peer_card_caption
        for config in ('<&"'*4000, 'x'*12000, '😀'*4000):
            value=peer_card_caption('<'*1200,config,'https://panel.example/u/token',qr=True)
            self.assertLessEqual(len(value),1024)
            self.assertLessEqual(len(value.encode('utf-16-le'))//2,1024)
            parser=BalancedHTML()
            parser.feed(value)
            self.assertFalse(parser.stack)
            self.assertNotIn('<'*5,value)

    def test_bytes_preview_matches_native_text_and_rejects_invalid_links(self):
        from telegram_peer_cards import peer_card_caption
        config='[Interface]\n# نام مشتری\nAddress = 10.0.0.2/32'
        self.assertEqual(peer_card_caption('client',config,'https://panel.example/u/token'),
                         peer_card_caption('client',config.encode(),'https://panel.example/u/token'))
        for url in ('javascript:alert(1)','not-a-link','http://[invalid'):
            value=peer_card_caption('client',config,url)
            self.assertIn('Unavailable',value)
            self.assertNotIn(url,value)

    def test_native_send_peers_uses_shared_cards_and_preserves_complete_file(self):
        import ast
        import asyncio
        import io
        from pathlib import Path
        from types import SimpleNamespace
        from telegram_peer_cards import bundle_keyboard,peer_card_caption,safe_peer_name
        source=Path(__file__).resolve().parents[1]/'telegram_bot.py'
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        nodes=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
               and node.name in {'send_peers','_bundle_kb'}]
        self.assertEqual(len(nodes),2)
        module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])
        calls=[]
        class Message:
            async def reply_document(self,**kwargs):
                calls.append(('document',kwargs))
            async def reply_photo(self,**kwargs):
                calls.append(('photo',kwargs))
        config='[Interface]\n# <unsafe>&\nAddress = 10.0.0.2/32'
        peer={'id':7,'name':'client<bad>'}
        url='https://panel.example/u/native'
        namespace={'io':io,'safe_peer_name':safe_peer_name,'peer_card_caption':peer_card_caption,
            'bundle_keyboard':bundle_keyboard,'peer_by_id':lambda pid:peer,
            'get_shortlink':lambda pid,p:url,'_peer_config':lambda pid,short:config,
            '_peer_qr':lambda pid,cfg:b'PNG-native','InputFile':lambda stream,filename:SimpleNamespace(data=stream.getvalue(),filename=filename),
            'ParseMode':SimpleNamespace(HTML='HTML'),
            'InlineKeyboardButton':lambda text,callback_data:{'text':text,'callback_data':callback_data},
            'InlineKeyboardMarkup':lambda rows:{'inline_keyboard':rows}}
        exec(compile(ast.fix_missing_locations(module),str(source),'exec'),namespace)
        asyncio.run(namespace['send_peers'](SimpleNamespace(effective_message=Message()),7))
        self.assertEqual([item[0] for item in calls],['document','photo'])
        self.assertEqual(calls[0][1]['document'].data,config.encode())
        self.assertEqual(calls[0][1]['document'].filename,'client_bad.conf')
        self.assertEqual(calls[0][1]['caption'],peer_card_caption(peer['name'],config,url))
        self.assertEqual(calls[1][1]['caption'],peer_card_caption(peer['name'],config,url,qr=True))
        self.assertEqual(calls[1][1]['reply_markup'],bundle_keyboard(7))
        # Exercise the native SDK BadRequest boundary with the same fake IO.
        import logging
        class PhotoBadRequest(Exception):
            pass
        class FailingPhoto(Message):
            async def reply_photo(self,**kwargs):
                raise PhotoBadRequest('simulated Telegram 400')
        namespace.update(BadRequest=PhotoBadRequest,logging=logging,__name__='native_card_test')
        calls.clear()
        with self.assertLogs('native_card_test',level='WARNING'):
            asyncio.run(namespace['send_peers'](SimpleNamespace(effective_message=FailingPhoto()),7))
        self.assertEqual([item[0] for item in calls],['document','document'])
        self.assertEqual(calls[1][1]['document'].filename,'client_bad-QR.png')
        self.assertEqual(calls[1][1]['document'].data,b'PNG-native')


if __name__=='__main__':
    unittest.main()
