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
    def test_nine_real_menu_buttons_and_https_launcher(self):
        self.send('/start');self.bot.flush_outbox()
        markup=self.transport.calls[-1][1]['reply_markup'];buttons=[b for row in markup['inline_keyboard'] for b in row]
        self.assertEqual(len(buttons),9)
        self.assertEqual(buttons[-1]['web_app']['url'],'https://portal.example.test/telegram-app')
        for action in ['shop','wallet','subscriptions','referrals','guides','downloads','support']:
            self.send(data=action)
        self.bot.flush_outbox()
        self.assertTrue(any('https://t.me/wgportal_test_bot?start=ref_' in str(call) for call in self.transport.calls))
        self.assertTrue(any('https://www.wireguard.com/install/' in str(call) for call in self.transport.calls))
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
    def test_private_identity_only_and_restart_dedup(self):
        self.assertFalse(self.bot.handle_update(self.update('/start',kind='group')))
        update=self.send('/start')
        fresh=PublicBot(self.service,self.transport,'https://portal.example.test',lambda:TOKEN,clock=lambda:self.now)
        fresh.handle_update(update)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM commerce_public_outbox').fetchone()[0],1)
        altered=self.update('/start',data='shop');altered['callback_query']['from']['id']=99
        self.assertFalse(self.bot.handle_update(altered))
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
