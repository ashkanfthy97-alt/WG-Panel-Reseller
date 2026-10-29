"""Native-looking reseller bot flows, with simulated Telegram and native IO."""
import asyncio
import unittest
from unittest.mock import patch
import test_reseller_bot as fixture
from portal_bot_runtime import AsyncBotRuntime
from portal_reseller_bot import BotError


class GuidedBotChecks(unittest.TestCase):
    setUp = fixture.BotChecks.setUp
    post = fixture.BotChecks.post
    account = fixture.BotChecks.account
    login = fixture.BotChecks.login
    configure = fixture.BotChecks.configure
    update = fixture.BotChecks.update

    def callback(self,data,update_id=2000,owner=987654321):
        return {'update_id':update_id,'callback_query':{'id':str(update_id),'data':data,
            'from':{'id':owner},'message':{'chat':{'type':'private','id':owner}}}}

    def wizard(self, row, texts, start=1000):
        answers = []
        for index, text in enumerate(texts):
            answers.append(self.manager.command(row, self.update(text, update_id=start + index)))
        return answers

    def test_unknown_text_and_addressed_commands_use_current_inline_menu(self):
        row,_=self.configure()
        expected=self.manager.menu()
        for index,text in enumerate(('لس','unrecognized text','/start@fixture_bot','/help@fixture_bot')):
            answer=self.manager.command(row,self.update(text,update_id=6000+index))
            self.assertEqual(answer[0],'messages')
            self.assertEqual(answer[1][0]['reply_markup'],expected)
            self.assertNotIn('/new name',answer[1][0]['text'])
            self.assertEqual(len(self.rows),0)

    def test_main_menu_has_exact_four_emoji_actions(self):
        row,_ = self.configure()
        answer = self.manager.command(row,self.update('/start'))
        buttons = answer[1][0]['reply_markup']['inline_keyboard']
        self.assertEqual([item['text'] for line in buttons for item in line],
            ['➕ ساخت PEER','👥 ساخت نماینده','📦 ساخت انبوه','⚙️ تنظیمات'])

    def test_peer_wizard_persian_digits_duplicate_final_and_no_media_receipt(self):
        row,_ = self.configure()
        answer = self.wizard(row,['➕ ساخت PEER','مشتری','۳۰','۱۰'])[-1]
        self.assertEqual(answer[0],'delivery')
        self.assertEqual(len(self.rows),1)
        again = self.manager.command(row,self.update('۱۰',update_id=1003))
        self.assertEqual(again[0],'delivery')
        self.assertEqual(len(self.rows),1)
        self.assertEqual(self.manager.completed_updates[(row['reseller_id'],row['revision'],1003)][1],1)
        items=answer[1]
        self.assertEqual(items[0]['document'],b'native-config')
        self.assertEqual([item['method'] for item in items],['sendDocument','sendPhoto'])
        self.assertTrue(items[1]['photo'].startswith(b'\x89PNG'))
        self.assertFalse(any('text' in item for item in items))
        for item in items:
            self.assertIn('<pre><code>native-config</code></pre>',item['caption'])
            self.assertIn('/u/opaque-user-token',item['caption'])
            self.assertEqual(item['parse_mode'],'HTML')
        self.assertTrue(all(len(item.get('caption',''))<=1024 for item in items))

    def test_private_owner_cancel_and_revision_do_not_resume_wizard(self):
        row,_ = self.configure()
        self.wizard(row,['➕ ساخت PEER','customer'])
        self.assertIsNone(self.manager.command(row,self.update('30',owner=12)))
        self.assertIsNone(self.manager.command(row,self.update('30',chat_type='group')))
        self.manager.command(row,self.update('/cancel',update_id=1002))
        self.wizard(row,['30','10'],start=1010)
        self.assertFalse(self.rows)
        self.wizard(row,['➕ ساخت PEER','customer'],start=1020)
        rotated,_=self.configure()
        self.wizard(rotated,['30','10'],start=1030)
        self.assertFalse(self.rows)
        self.assertFalse(self.manager.wizards)

    def test_child_wizard_only_two_fields_and_password_not_retained_or_echoed(self):
        row,_=self.configure()
        password='unique-child-secret-password'
        answers=self.wizard(row,['👥 ساخت نماینده','child',password])
        self.assertEqual(len(self.native_interface_calls),1)
        children=self.store.children(self.parent['id'])
        self.assertEqual(len(children),1)
        self.assertFalse(children[0]['active'])
        self.assertNotIn(password,str(answers))
        self.assertNotIn(password,str(self.manager.wizards))
        self.assertNotIn(password,str(self.manager.completed_updates))
        self.assertIn('@AshkanfathiOfficial',str(answers[-1]))
        self.manager.command(row,self.update(password,update_id=1002))
        self.assertEqual(len(self.store.children(self.parent['id'])),1)

    def test_expired_child_wizard_drops_password_state(self):
        row,_=self.configure()
        self.wizard(row,['👥 ساخت نماینده','child'])
        key=(row['reseller_id'],row['revision'])
        self.manager.wizards[key]['expires']=0
        self.manager.command(row,self.update('expired-child-password',update_id=1002))
        self.assertFalse(self.manager.wizards)
        self.assertFalse(self.store.children(self.parent['id']))

    def test_bulk_partial_uncertain_summary_and_replay_do_not_repeat_post(self):
        row,_=self.configure()
        original=self.manager.dispatch
        calls=[]
        def dispatch(snapshot,method,path,**kwargs):
            if path=='/reseller/api/peers/bulk':
                calls.append(kwargs)
                return 409,{'created':[{'id':1,'name':'batch-1'}],
                    'failed':[{'index':2,'error':'create_uncertain'}], 'uncertain':True},b''
            return original(snapshot,method,path,**kwargs)
        # Seed an owned native Peer so bundle retrieval exercises real ownership.
        self.manager.command(row,self.update('/new seeded | 30 | 10',update_id=990))
        with patch.object(self.manager,'dispatch',side_effect=dispatch):
            answer=self.wizard(row,['📦 ساخت انبوه','batch','2','30','10'])[-1]
            replay=self.manager.command(row,self.update('10',update_id=1004))
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['payload'],{'name_prefix':'batch','count':2,'days':30,'volume_gb':10})
        self.assertTrue(calls[0]['operation'].startswith('telegram-update-'))
        self.assertEqual(answer[0],'delivery')
        summary=answer[1][0]['text']
        self.assertIn('Created: 1',summary)
        self.assertIn('Failed: 1',summary)
        self.assertIn('Uncertain',summary)
        self.assertEqual(replay[1][0]['text'],summary)
        receipt=self.manager.completed_updates[(row['reseller_id'],row['revision'],1004)][1]
        self.assertEqual(receipt['ids'],[1])
        self.assertNotIn('document',str(receipt))

    def test_sync_and_async_delivery_keep_full_file_and_safe_payload(self):
        row,token=self.configure()
        self.manager.command(row,self.update('/new customer | 30 | 10'))
        answer=self.manager.bundle(row,1)
        sync_calls=[]
        class Sync:
            def call(inner,token,method,payload=None,**files):
                sync_calls.append((method,payload,files))
        with patch.object(self.manager,'transport',return_value=Sync()):
            self.manager.send_answer(row,token,answer)
        async_calls=[]
        class Async:
            async def call(inner,token,method,payload=None,**files):
                async_calls.append((method,payload,files))
        async def send():
            runtime=AsyncBotRuntime(self.manager,Async())
            try:
                await runtime.send_answer(row,token,answer)
            finally:
                await runtime.close()
        asyncio.run(send())
        self.assertEqual(sync_calls,async_calls)
        self.assertEqual(sync_calls[0][2]['document'],b'native-config')
        self.assertEqual(sync_calls[0][1]['filename'],'customer.conf')
        self.assertTrue(any(method=='sendPhoto' for method,_,_ in sync_calls))
        self.assertTrue(all(payload['chat_id']==987654321 and payload.get('parse_mode')=='HTML'
                            for _,payload,_ in sync_calls))

    def test_long_config_file_is_not_truncated_and_oversized_preview_omitted(self):
        row,_=self.configure()
        self.manager.command(row,self.update('/new long-config | 30 | 10'))
        original=self.manager.dispatch
        content=b'[Interface]\n'+b'# full configuration\n'*400
        def dispatch(snapshot,method,path,**kwargs):
            if path.endswith('/config'):
                return 200,None,content
            return original(snapshot,method,path,**kwargs)
        with patch.object(self.manager,'dispatch',side_effect=dispatch):
            answer=self.manager.bundle(row,1)
        self.assertEqual(answer[1][0]['document'],content)
        self.assertFalse(any('text' in item and 'full configuration' in item['text'] for item in answer[1]))
        self.assertTrue(all(len(item['caption'].encode('utf-16-le'))//2<=1024 for item in answer[1]))

    def test_inline_navigation_cancel_refresh_edit_back_preserve_ownership(self):
        row,_=self.configure()
        prompt=self.manager.command(row,self.callback('reseller:peer'))
        self.assertEqual(prompt[0],'messages')
        cancel=prompt[1][0]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertEqual(cancel,'reseller:cancel')
        self.manager.command(row,self.callback(cancel,2001))
        self.assertFalse(self.manager.wizards)
        self.manager.command(row,self.update('/new card | 30 | 10'))
        count=len(self.rows)
        self.assertEqual(self.manager.command(row,self.callback('peer:bundle:1',2002))[0],'delivery')
        self.assertEqual(self.manager.command(row,self.callback('peer:edit:1',2003))[0],'messages')
        self.assertEqual(self.manager.command(row,self.callback('peers:menu',2004))[0],'messages')
        self.assertEqual(len(self.rows),count)
        self.assertIsNone(self.manager.command(row,self.callback('peer:bundle:1',2005,owner=12)))
        self.rows[0]['public_key']='replaced-key'
        for action in ('peer:bundle:1','peer:edit:1'):
            self.assertIn('unavailable',self.manager.command(row,self.callback(action,2006))[1])

    def test_photo_bad_request_fallback_and_other_errors_are_not_retried(self):
        row,token=self.configure()
        self.manager.command(row,self.update('/new fallback | 30 | 10'))
        answer=self.manager.bundle(row,1)
        for error_code in ('telegram_bad_request','telegram_invalid_token','telegram_rate_limited','telegram_connection_failed'):
            for asynchronous in (False,True):
                with self.subTest(error=error_code,asynchronous=asynchronous):
                    calls=[]
                    def record(method,payload,files):
                        calls.append((method,payload,files))
                        if method=='sendPhoto':
                            raise BotError(error_code)
                    class Sync:
                        def call(inner,token,method,payload=None,**files):
                            record(method,payload,files)
                    class Async:
                        async def call(inner,token,method,payload=None,**files):
                            record(method,payload,files)
                    async def run():
                        runtime=AsyncBotRuntime(self.manager,Async())
                        try:
                            await runtime.send_answer(row,token,answer)
                        finally:
                            await runtime.close()
                    def send():
                        if asynchronous:
                            asyncio.run(run())
                        else:
                            with patch.object(self.manager,'transport',return_value=Sync()):
                                self.manager.send_answer(row,token,answer)
                    if error_code=='telegram_bad_request':
                        send()
                        self.assertEqual([item[0] for item in calls],['sendDocument','sendPhoto','sendDocument'])
                        self.assertEqual(calls[-1][2]['document'],answer[1][1]['photo'])
                        self.assertEqual(calls[-1][1]['caption'],answer[1][1]['caption'])
                        self.assertEqual(calls[-1][1]['filename'],'fallback-QR.png')
                    else:
                        with self.assertRaises(BotError):
                            send()
                        self.assertEqual([item[0] for item in calls],['sendDocument','sendPhoto'])


if __name__=='__main__':
    unittest.main()
