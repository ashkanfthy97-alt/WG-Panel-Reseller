"""Public bot against the real commerce domain and fake Telegram transport."""
import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portal_commerce_store import Store
from portal_commerce_service import CommerceService
from portal_public_bot import PublicBot
TOKEN='123456:'+('A'*35)

class FakeTelegram:
    def __init__(self):self.calls=[];self.updates=[];self.fail=False
    def call(self,token,method,payload=None,**files):
        assert token==TOKEN
        self.calls.append((method,payload,files))
        if method=='getUpdates':return self.updates
        if method=='getMe':return {'id':123456,'username':'wgportal_test_bot','is_bot':True}
        if self.fail:
            error=ValueError('no secret error reflected');error.retry_after=60;raise error
        return {'message_id':len(self.calls)}

class BotChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1000;self.store=Store(Path(self.tmp.name)/'bot.sqlite3')
        self.service=CommerceService(self.store,clock=lambda:self.now)
        self.transport=FakeTelegram();self.bot=PublicBot(self.service,self.transport,'https://portal.example.test',lambda:TOKEN,clock=lambda:self.now)
        self.sequence=0
        self.product=self.service.admin_action('owner','save_product',{'name':'طرح آزمایش','category':'economy','traffic_gb':10,'days':30,'devices':1,'price_toman':100000,'active':True,'target':{'scope':'local','iface_id':1},'pricing':{'device_options':[1,2],'additional_device_toman':5000}})['product']
        with self.store.db(write=True) as db:db.execute("INSERT INTO payment_destinations(card_number,holder_name,bank_name,instructions,active,created_at,updated_at) VALUES('0000000000000000','fixture','fixture','فقط آزمایش',1,1,1)")
    def update(self,text='',data=None,uid=42,kind='private',update_id=None):
        self.sequence+=1;message={'text':text,'from':{'id':uid,'first_name':'مشتری'},'chat':{'id':uid,'type':kind}}
        update={'update_id':update_id or self.sequence}
        update['message' if data is None else 'callback_query']=message if data is None else {'id':'cb'+str(self.sequence),'from':message['from'],'message':message,'data':data}
        return update
    def send(self,text='',data=None):
        update=self.update(text,data);self.bot.handle_update(update);return update
    def customer(self):
        with self.store.db() as db:return dict(db.execute('SELECT * FROM customers WHERE telegram_id=42').fetchone())
    def purchase(self):
        self.send('/start');self.send(data='shop');self.send(data='product:'+str(self.product['id']))
        self.send(data='traffic:10');self.send(data='duration:30');self.send(data='devices:1')
        return self.send(data='buy:card')
    def test_removes_legacy_keyboard_and_keeps_inline_menu_and_https_launcher(self):
        self.send('/start');self.bot.flush_outbox()
        self.assertEqual(self.transport.calls[0][1]['reply_markup'],{'remove_keyboard':True})
        self.assertTrue(self.transport.calls[0][1]['disable_notification'])
        markup=self.transport.calls[-1][1]['reply_markup'];buttons=[b for row in markup['inline_keyboard'] for b in row]
        self.assertEqual(len(buttons),10)
        self.assertEqual(buttons[-1]['web_app']['url'],'https://portal.example.test/telegram-app')
        self.send('🚀 ورود به پنل کاربری');self.bot.flush_outbox()
        method,launcher,_=self.transport.calls[-1]
        self.assertEqual((method,launcher['message_id']),('editMessageText',2))
        self.assertEqual(launcher['reply_markup']['inline_keyboard'][0][0]['web_app']['url'],
                         'https://portal.example.test/telegram-app')
        for action in ['shop','wallet','subscriptions','referrals','guides','downloads','support']:
            self.send(data=action)
        self.bot.flush_outbox()
        self.assertTrue(any('https://t.me/wgportal_test_bot?start=ref_' in str(call) for call in self.transport.calls))
        self.assertTrue(any('https://www.wireguard.com/install/' in str(call) for call in self.transport.calls))

    def test_old_outbox_item_cannot_restore_a_reply_keyboard(self):
        self.send('/start')
        with self.store.db(write=True) as db:
            existing=db.execute('SELECT MIN(id) FROM commerce_public_outbox').fetchone()[0]
            legacy={'text':'legacy setup','reply_markup':{'keyboard':[[{'text':'legacy'}]]}}
            db.execute('UPDATE commerce_public_outbox SET payload=? WHERE id=?',(json.dumps(legacy),existing))
        self.bot.flush_outbox()
        self.assertEqual(self.transport.calls[0][1]['reply_markup'],{'remove_keyboard':True})
        self.assertIn('inline_keyboard',self.transport.calls[1][1]['reply_markup'])
    def test_keyboard_purchase_categories_and_back_navigation(self):
        self.send('/start')
        self.send(data='shop')
        self.send(data='categoryshop:economy')
        self.send(data='product:'+str(self.product['id']))
        self.send(data='traffic:10')
        self.send(data='duration:30')
        self.send(data='devices:1')
        self.send(data='back:devices')
        self.send(data='back:duration')
        self.bot.flush_outbox()
        self.assertEqual([method for method,_,_ in self.transport.calls],
                         ['sendMessage']*2+['editMessageText']*(len(self.transport.calls)-2))
        replies=[payload for _,payload,_ in self.transport.calls[1:]]
        self.assertEqual({reply['message_id'] for reply in replies[1:]},{2})
        self.assertIn('حجم، مدت و تعداد دستگاه',replies[1]['text'])
        options=lambda reply:{button.get('callback_data') for row in reply['reply_markup']['inline_keyboard'] for button in row}
        self.assertIn('categoryshop:economy',options(replies[1]))
        self.assertIn('product:'+str(self.product['id']),options(replies[2]))
        self.assertIn('buy:card',options(replies[-3]))
        self.assertIn('devices:2',options(replies[-2]))
        self.assertIn('duration:30',options(replies[-1]))
    def test_poll_sends_ready_replies_before_waiting_and_answers_each_customer(self):
        self.send('/start')
        self.transport.updates=[self.update(data='shop',uid=84),self.update(data='shop',uid=126)]
        self.bot.poll_once(deliver_notifications=False)
        calls=self.transport.calls
        self.assertEqual([method for method,_,_ in calls],['sendMessage','sendMessage','getUpdates','answerCallbackQuery','sendMessage','answerCallbackQuery','sendMessage'])
        self.assertEqual([payload['chat_id'] for method,payload,_ in calls if method=='sendMessage'],[42,42,84,126])
    def test_public_access_for_multiple_nonmanager_customers_with_isolated_purchase_state(self):
        self.bot.administrator_ids_provider=lambda:[555]
        customers=[42,84,126]
        for uid in customers+[555]:
            self.assertTrue(self.bot.handle_update(self.update('/start',uid=uid)))
        self.bot.flush_outbox()
        menus=[payload for method,payload,_ in self.transport.calls if method=='sendMessage' and 'به WG Portal خوش آمدید' in payload.get('text','')]
        self.assertEqual({payload['chat_id'] for payload in menus},set(customers+[555]))
        self.assertTrue(all(len([item for row in payload['reply_markup']['inline_keyboard'] for item in row])==10 for payload in menus))
        steps=['shop','product:'+str(self.product['id']),'traffic:10','duration:30']
        for step in steps:
            for uid in customers:self.assertTrue(self.bot.handle_update(self.update(data=step,uid=uid)))
        for uid,devices in [(42,1),(84,2),(126,1)]:
            self.bot.handle_update(self.update(data='devices:'+str(devices),uid=uid))
        for uid in reversed(customers):self.bot.handle_update(self.update(data='buy:card',uid=uid))
        with self.store.db() as db:
            rows=list(db.execute('SELECT c.telegram_id,o.total_toman,o.snapshot FROM orders o JOIN customers c ON c.id=o.customer_id'))
        self.assertEqual({row['telegram_id']:row['total_toman'] for row in rows},{42:100000,84:105000,126:100000})
        self.assertEqual({row['telegram_id']:json.loads(row['snapshot'])['devices'] for row in rows},{42:1,84:2,126:1})
        self.service.admin_action('owner','save_settings',{'trial_enabled':True,'trial_target':{'scope':'local','iface_id':1}})
        for uid in customers[:2]:self.bot.handle_update(self.update(data='trial',uid=uid))
        self.bot.handle_update(self.update(data='trial',uid=42))
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM trial_claims').fetchone()[0],2)

    def test_each_private_chat_edits_its_own_menu(self):
        for uid in (42,84):
            self.bot.handle_update(self.update('/start',uid=uid))
        self.bot.flush_outbox()
        for uid in (84,42):
            self.bot.handle_update(self.update('💰 کیف پول',uid=uid))
        self.bot.flush_outbox()
        edits=[(payload['chat_id'],payload['message_id'])
               for method,payload,_ in self.transport.calls if method=='editMessageText']
        self.assertEqual(edits,[(84,4),(42,2)])

    def test_start_recovers_a_buried_menu_and_buttons_edit_the_replacement(self):
        self.send('/start')
        self.bot.flush_outbox()
        self.send(data='wallet')
        self.bot.flush_outbox()
        self.send('/start')
        self.bot.flush_outbox()
        self.send(data='shop')
        self.bot.flush_outbox()
        self.send(data='home')
        self.bot.flush_outbox()
        self.send('/menu')
        self.bot.flush_outbox()
        self.assertEqual([method for method,_,_ in self.transport.calls],[
            'sendMessage','sendMessage','editMessageText','sendMessage','sendMessage',
            'editMessageText','editMessageText','sendMessage','sendMessage',
        ])
        self.assertEqual([payload['message_id'] for method,payload,_ in self.transport.calls
                          if method=='editMessageText'],[2,5,5])

    def test_rejected_mini_app_button_does_not_stop_the_public_menu(self):
        from portal_reseller_bot import BotError
        call=self.transport.call
        def reject_web_app(token,method,payload=None,**files):
            markup=payload.get('reply_markup',{})
            rows=markup.get('keyboard', markup.get('inline_keyboard', []))
            if method=='sendMessage' and any('web_app' in button for row in rows for button in row):
                raise BotError('telegram_miniapp_button_rejected')
            return call(token,method,payload,**files)
        self.transport.call=reject_web_app
        self.send('/start')
        self.assertEqual(self.bot.flush_outbox(),2)
        self.assertEqual(self.transport.calls[0][1]['reply_markup'],{'remove_keyboard':True})
        self.assertIn('خوش آمدید',self.transport.calls[-1][1]['text'])
        self.send(data='shop')
        self.assertEqual(self.bot.flush_outbox(),1)
        self.assertEqual(self.transport.calls[-1][0],'editMessageText')

    def test_malformed_update_does_not_block_the_next_customer(self):
        malformed_profile=self.update('/start',update_id=2)
        malformed_profile['message']['from']['username']=None
        self.transport.updates=[{'update_id':1,'callback_query':['invalid']},
                                malformed_profile,self.update('/start',uid=84,update_id=3)]
        self.assertEqual(self.bot.poll_once(deliver_notifications=False),4)
        self.assertEqual([payload['chat_id'] for method,payload,_ in self.transport.calls
                          if method=='sendMessage'],[42,42,84,84])

    def test_public_customer_cannot_retrieve_another_customers_configuration(self):
        for uid in [42,84]:self.bot.handle_update(self.update('/start',uid=uid))
        first=self.customer()['id']
        with self.store.db(write=True) as db:
            sid=db.execute('INSERT INTO subscriptions(customer_id,status,snapshot,native_snapshot,trial,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (first,'active',json.dumps({'product_name':'آزمایش','devices':1,'traffic_gb':10}),json.dumps({'peers':[{'id':7,'public_key':'fixture-public-key'}]}),0,self.now,self.now)).lastrowid
        delivered=[]
        def artifact(cid,subscription_id,peer_id,kind):
            delivered.append((cid,subscription_id,peer_id,kind));return b'private-test-config','text/plain'
        self.bot.artifact_provider=artifact
        self.bot.handle_update(self.update(data='config:'+str(sid)+':7',uid=84))
        self.bot.handle_update(self.update(data='config:'+str(sid)+':7',uid=42))
        self.bot.flush_outbox()
        self.assertEqual(delivered,[(first,sid,7,'config')])
        documents=[payload for method,payload,_ in self.transport.calls if method=='sendDocument']
        self.assertEqual([payload['chat_id'] for payload in documents],[42])
    def test_purchase_is_durable_idempotent_and_payment_destination_real(self):
        last=self.purchase();self.bot.handle_update(last)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0],1)
            payment=dict(db.execute('SELECT * FROM payments').fetchone())
        self.assertEqual(payment['status'],'pending_payment')
        self.bot.flush_outbox()
        self.assertTrue(any('0000000000000000' in str(call) and 'fixture' in str(call) for call in self.transport.calls))
        self.send(data='receipt:'+str(payment['id']));self.send('شماره پیگیری آزمایشی')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT status FROM payments').fetchone()[0],'pending_review')
        self.assertEqual(self.service.admin_action('owner','dashboard')['active_subscriptions'],0)

    def test_telegram_photo_completes_existing_text_receipt_and_replay_is_safe(self):
        self.purchase()
        with self.store.db() as db:
            pid=db.execute('SELECT id FROM payments').fetchone()['id']
        self.send(data='receipt:'+str(pid))
        self.send('tracking-test')
        downloads=[]
        def download(token,file_id):
            downloads.append((token,file_id))
            return b'test image'
        self.transport.download_file=download
        self.bot.receipt_image_provider=lambda body,identity: 'a'*48+'.png'
        update=self.update()
        update['message']['photo']=[{'file_id':'small-test'},{'file_id':'large-test'}]
        self.assertTrue(self.bot.handle_update(update))
        self.assertTrue(self.bot.handle_update(update))
        self.assertEqual(downloads,[(TOKEN,'large-test')])
        with self.store.db() as db:
            row=db.execute('SELECT status,receipt_reference,transaction_text FROM payments WHERE id=?',(pid,)).fetchone()
        self.assertEqual(tuple(row),('pending_review','a'*48+'.png','tracking-test'))

    def test_receipt_document_selects_own_payment_before_downloading(self):
        self.send('/start')
        cid=self.customer()['id']
        own=[self.service.customer_action(cid,'topup',{'amount_toman':amount},request_id='fixture-topup-'+str(amount))['payment']['id']
             for amount in (100000,200000)]
        stranger=self.service.customer_from_telegram({'id':84})['id']
        foreign=self.service.customer_action(stranger,'topup',{'amount_toman':100000},request_id='fixture-topup-foreign')['payment']['id']
        downloads=[]
        def download(token,file_id):
            downloads.append(file_id)
            return b'test image'
        self.transport.download_file=download
        self.bot.receipt_image_provider=lambda body,identity: 'b'*48+'.png'
        update=self.update()
        update['message'].update(document={'file_id':'document-test','mime_type':'image/png'},caption='caption-test')
        self.bot.handle_update(update)
        self.send(data='receiptpick:'+str(foreign))
        self.assertEqual(downloads,[])
        self.send(data='receiptpick:'+str(own[0]))
        self.assertEqual(downloads,['document-test'])
        with self.store.db() as db:
            saved=[tuple(row) for row in db.execute('SELECT id,receipt_reference,transaction_text FROM payments WHERE receipt_reference IS NOT NULL')]
        self.assertEqual(saved,[(own[0],'b'*48+'.png','caption-test')])
        self.bot.flush_outbox()
        button=self.transport.calls[-1][1]['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['callback_data'],'receipt:'+str(own[0]))
        self.send(data=button['callback_data'])
        self.send('new-tracking-test')
        with self.store.db() as db:
            row=db.execute('SELECT receipt_reference,transaction_text FROM payments WHERE id=?',(own[0],)).fetchone()
        self.assertEqual(tuple(row),('b'*48+'.png','new-tracking-test'))
    def test_private_identity_only_and_restart_dedup(self):
        self.assertFalse(self.bot.handle_update(self.update('/start',kind='group')))
        update=self.send('/start')
        fresh=PublicBot(self.service,self.transport,'https://portal.example.test',lambda:TOKEN,clock=lambda:self.now)
        fresh.handle_update(update)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM commerce_public_outbox').fetchone()[0],2)
        altered=self.update('/start',data='shop');altered['callback_query']['from']['id']=99
        self.assertFalse(self.bot.handle_update(altered))
    def test_reuses_legacy_callback_message_and_typed_reply_after_restart(self):
        update=self.update('/start ref_ignored',data='topup')
        update['callback_query']['message'].update(message_id=97, **{'from':{'id':123456,'is_bot':True}})
        self.bot.handle_update(update);self.bot.flush_outbox()
        self.assertEqual(self.transport.calls[-1][0],'editMessageText')
        self.assertEqual(self.transport.calls[-1][1]['message_id'],97)
        self.bot=PublicBot(self.service,self.transport,'https://portal.example.test',lambda:TOKEN,clock=lambda:self.now)
        self.send('۱۰۰۰۰۰');self.bot.flush_outbox()
        self.assertEqual(self.transport.calls[-1][0],'editMessageText')
        self.assertEqual(self.transport.calls[-1][1]['message_id'],97)

    def test_replaces_uneditable_message_once_and_remembers_replacement(self):
        self.send('/start');self.bot.flush_outbox()
        original=self.transport.call
        attempted=[]
        def deleted(token,method,payload=None,**files):
            if method=='editMessageText' and payload['message_id']==2:
                attempted.append(payload['message_id'])
                error=ValueError('simulated missing message')
                error.code='telegram_message_uneditable'
                raise error
            return original(token,method,payload,**files)
        self.transport.call=deleted
        self.send(data='wallet');self.bot.flush_outbox()
        self.send(data='home');self.bot.flush_outbox()
        self.assertEqual(attempted,[2])
        self.assertEqual([method for method,_,_ in self.transport.calls],['sendMessage','sendMessage','sendMessage','editMessageText'])
        self.assertEqual(self.transport.calls[-1][1]['message_id'],3)

    def test_retry_of_old_screen_cannot_replace_a_delivered_newer_screen(self):
        self.send('/start');self.bot.flush_outbox()
        self.transport.fail=True
        self.send(data='wallet');self.bot.flush_outbox()
        self.transport.fail=False
        self.send(data='support');self.bot.flush_outbox()
        self.now+=61
        self.assertEqual(self.bot.flush_outbox(),0)
        self.assertIn('پشتیبانی',self.transport.calls[-1][1]['text'])

    def test_trial_opens_explanation_before_registering_request(self):
        self.service.admin_action('owner','save_settings',{'trial_enabled':True,'trial_target':{'scope':'local','iface_id':1}})
        self.send('/start');self.send(data='trialinfo');self.bot.flush_outbox()
        method,payload,_=self.transport.calls[-1]
        self.assertEqual(method,'editMessageText')
        self.assertIn('پیش از خرید',payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],'trial')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM trial_claims').fetchone()[0],0)
        self.send(data='trial');self.bot.flush_outbox()
        self.assertEqual(self.transport.calls[-1][0],'editMessageText')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM trial_claims').fetchone()[0],1)

    def test_ticket_history_and_reply_prompt_use_one_bounded_screen(self):
        self.send('/start');self.bot.flush_outbox()
        original=self.service.customer_action
        def action(customer_id,action,payload=None,request_id=None):
            if action=='ticket_messages':
                return {'messages':[{'body':'📣'*4000},{'body':'آخرین پیام'}]}
            return original(customer_id,action,payload,request_id)
        self.service.customer_action=action
        self.send(data='ticket:1');self.bot.flush_outbox()
        self.assertEqual([method for method,_,_ in self.transport.calls],['sendMessage','sendMessage','editMessageText'])
        reply=self.transport.calls[-1][1]
        self.assertEqual(reply['message_id'],2)
        self.assertIn('آخرین پیام',reply['text'])
        self.assertIn('برای پاسخ',reply['text'])
        self.assertLessEqual(len(reply['text'].encode('utf-16-le'))//2,4096)
    def test_outbox_rate_limit_retries_without_recreating_order(self):
        self.purchase();self.transport.fail=True
        self.assertEqual(self.bot.flush_outbox(),0)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM commerce_public_outbox LIMIT 1').fetchone();self.assertEqual(row['next_at'],1060)
        self.transport.fail=False;self.now=1061;self.assertGreater(self.bot.flush_outbox(),0)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0],1)
    def test_support_and_topup_have_real_domain_effects(self):
        self.send('/start');self.send(data='support');self.send(data='ticketnew');self.send(data='category:connection');self.send('اتصال آزمایشی')
        self.send(data='topup');self.send('۱۰۰۰۰۰')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM support_tickets').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT amount_toman FROM payments WHERE kind='topup'").fetchone()[0],100000)
    def test_poll_offsets_and_notifications_are_durable(self):
        self.transport.updates=[self.update('/start',update_id=77)]
        self.bot.poll_once();self.bot.poll_once()
        with self.store.db() as db:self.assertEqual(db.execute('SELECT offset FROM commerce_public_offsets').fetchone()[0],78)
        self.assertEqual(len([call for call in self.transport.calls if call[0]=='sendMessage' and 'حساب شما' in call[1].get('text','')]),1)
    def test_artifacts_outbox_contains_references_never_configuration(self):
        self.send('/start');customer=self.customer()
        with self.store.db(write=True) as db:
            db.execute('INSERT INTO commerce_public_outbox(bot_id,customer_id,chat_id,payload) VALUES(?,?,?,?)',('123456',customer['id'],42,json.dumps({'artifact':{'subscription_id':1,'peer_id':5,'kind':'config'}})))
        self.bot.artifact_provider=lambda cid,sid,pid,kind:(b'[Interface]\nPrivateKey=private-fixture','text/plain')
        self.bot.flush_outbox()
        self.assertTrue(any(call[2].get('document',b'').endswith(b'private-fixture') for call in self.transport.calls))
        with self.store.db() as db:
            self.assertNotIn('private-fixture',' '.join(row['payload'] for row in db.execute('SELECT payload FROM commerce_public_outbox')))
    def test_administrator_notifications_and_long_announcements_retry_per_recipient(self):
        self.send('/start');customer=self.customer()
        self.bot.administrator_ids_provider=lambda:[555,666]
        with self.store.db(write=True) as db:
            self.service._notify(db,customer['id'],'announcement',{'title':'خبر مهم','body':'📣'*4000},'announcement-test')
        original=self.transport.call;failed=[False]
        def flaky(token,method,payload=None,**files):
            if method=='sendMessage' and payload.get('chat_id')==666 and not failed[0]:
                failed[0]=True;raise ValueError('simulated delivery failure')
            return original(token,method,payload,**files)
        self.transport.call=flaky;self.bot.notify_pending()
        self.now+=61;self.bot.notify_pending()
        admin_calls=[call for call in self.transport.calls if call[0]=='sendMessage' and call[1].get('chat_id')==555]
        self.assertEqual(len(admin_calls),1)
        self.assertEqual(admin_calls[0][1]['reply_markup']['inline_keyboard'][0][0]['url'],'https://portal.example.test/telegram-commerce')
        for method,payload,_ in self.transport.calls:
            if method=='sendMessage':self.assertLessEqual(len(payload['text'].encode('utf-16-le'))//2,4096)
    def test_activation_delivers_configuration_and_qr_once_without_storing_secrets(self):
        self.send('/start');cid=self.customer()['id'];original=self.service.customer_action
        def action(customer_id,action,payload=None,request_id=None):
            if action=='subscription':
                self.assertEqual(customer_id,cid);self.assertEqual(payload,{'id':9})
                return {'subscription':{'id':9,'peers':[{'id':5}]}}
            return original(customer_id,action,payload,request_id)
        self.service.customer_action=action
        self.bot.artifact_provider=lambda customer_id,sid,pid,kind:(b'protected-config' if kind=='config' else b'\x89PNGfixture','test')
        with self.store.db(write=True) as db:self.service._notify(db,cid,'subscription_activation',{'subscription_id':9},'activation-unique')
        self.bot.notify_pending();self.bot.notify_pending()
        self.assertEqual(len([c for c in self.transport.calls if c[0]=='sendDocument']),1)
        self.assertEqual(len([c for c in self.transport.calls if c[0]=='sendPhoto']),1)
        with self.store.db() as db:
            self.assertNotIn('protected-config',' '.join(str(dict(row)) for row in db.execute('SELECT * FROM notification_jobs')))

if __name__=='__main__':unittest.main()
