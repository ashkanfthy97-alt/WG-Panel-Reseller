import concurrent.futures
import json
from datetime import datetime, timezone
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portal_commerce_store import Store
from portal_commerce_service import CommerceService, DomainError


class Native:
    def __init__(self): self.created = {}; self.calls = 0
    def provision(self, operation_id, spec):
        self.calls += 1
        result = {'id': 100 + self.calls, 'status': 'active', 'peers': [{'id': 1000 + self.calls}], 'used_bytes': 0}
        self.created[operation_id] = result
        return result
    def reconcile(self, operation_id): return self.created.get(operation_id)
    def subscription(self, native_id): return {'id': native_id, 'status': 'active', 'used_bytes': 123}
    def suspend(self,operation_id,spec):return {'id':spec['existing_native_id'],'status':'suspended'}


class CommerceDomain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'commerce.sqlite3')
        self.native = Native(); self.s = CommerceService(self.store, self.native)
        self.a = self.s.customer_from_telegram({'id': 101, 'first_name': 'الف'})['id']
        self.b = self.s.customer_from_telegram({'id': 102, 'first_name': 'ب'})['id']
        self.product = self.s.admin_action('owner', 'save_product', {'name':'یک ماه', 'category':'economy','traffic_gb':10,'days':30,'devices':1,'price_toman':100000,'active':True,'target':{'scope':'local','iface_id':1}})['product']
    def action(self, who, action, value=None, key='temporary-request-key-1234'):
        return self.s.customer_action(who, action, value or {}, key)
    def funded(self, who, amount=500000):
        return self.s.admin_action('owner','wallet_adjustment',{'id':who,'amount_toman':amount,'reason':'آزمایش'},'adjustment-abcdef-'+str(who))
    def quote(self, who=None, **kw):
        return self.action(who or self.a,'quote',{'product_id':self.product['id'],**kw})['quote']
    def order(self, who=None, method='wallet', **kw):
        return self.action(who or self.a,'orders',{'quote_id':self.quote(who,**kw)['id'],'payment_method':method},'order-'+str(who or self.a)+'-abcdefgh')
    def test_prices_and_discount_are_server_side(self):
        self.s.admin_action('owner','save_discount',{'code':'PROMO','kind':'percent','value':10,'active':True,'max_uses':1,'per_customer':1})
        q=self.quote(discount_code='PROMO'); self.assertEqual(q['total_toman'],90000)
        with self.assertRaises(DomainError): self.quote(price_toman=1)
    def test_wallet_order_idempotency_and_native_once(self):
        self.funded(self.a)
        q=self.quote(); payload={'quote_id':q['id'],'payment_method':'wallet'}
        first=self.action(self.a,'orders',payload)
        second=self.action(self.a,'orders',payload)
        self.assertEqual(first['order']['id'],second['order']['id'])
        self.s.process_jobs(); self.s.process_jobs()
        self.assertEqual(self.native.calls,1)
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],400000)
    def test_customer_isolation(self):
        self.funded(self.a); result=self.order()
        with self.assertRaises(DomainError): self.action(self.b,'order',{'id':result['order']['id']})
    def test_duplicate_approval_topup_once(self):
        with self.store.db(write=True) as db:
            db.execute("INSERT INTO payment_destinations(card_number,holder_name,bank_name,instructions,active,created_at,updated_at) VALUES('0000000000000000','fixture','', '',1,1,1)")
        payment=self.action(self.a,'topup',{'amount_toman':250000})['payment']
        self.action(self.a,'submit_receipt',{'payment_id':payment['id'],'receipt_reference':'receipt-test.png','transaction_text':'تست'},'receipt-abcdefgh-1234')
        self.s.admin_action('owner','review_payment',{'id':payment['id'],'decision':'approved'},'review-abcdefgh-1234')
        self.s.admin_action('owner','review_payment',{'id':payment['id'],'decision':'approved'},'review-second-123456')
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],250000)
    def test_concurrent_wallet_spending(self):
        self.funded(self.a,100000)
        q1=self.quote();q2=self.quote()
        def spend(item):
            try: self.action(self.a,'orders',{'quote_id':item[1]['id'],'payment_method':'wallet'},'parallel-abcdefgh-'+str(item[0]));return True
            except DomainError:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(spend,enumerate([q1,q2])))
        self.assertEqual(sum(results),1);self.assertEqual(self.action(self.a,'wallet')['balance_toman'],0)
    def test_trial_once(self):
        self.s.admin_action('owner','save_settings',{'trial_enabled':True,'trial_target':{'scope':'local','iface_id':1}})
        self.action(self.a,'trial')
        with self.assertRaises(DomainError):self.action(self.a,'trial',key='another-trial-key-12345')
    def test_rejection_reason_and_support_isolation(self):
        ticket=self.action(self.a,'create_ticket',{'category':'payment','body':'سلام'})['ticket']
        with self.assertRaises(DomainError):self.action(self.b,'ticket_messages',{'id':ticket['id']})
        self.s.admin_action('owner','reply_ticket',{'id':ticket['id'],'body':'پاسخ'},'reply-abcdefg-12345')
        self.assertEqual(len(self.action(self.a,'ticket_messages',{'id':ticket['id']})['messages']),2)
    def test_ledger_is_immutable(self):
        self.funded(self.a)
        with self.assertRaises(Exception):
            with self.store.db(write=True) as db:db.execute('UPDATE wallet_ledger SET amount_toman=0')
    def test_suspended_customer_denied(self):
        self.s.admin_action('owner','suspend_customer',{'id':self.a,'suspended':True})
        with self.assertRaises(DomainError):self.action(self.a,'quote',{'product_id':self.product['id']})

    def test_destination_history_and_order_snapshot(self):
        first=self.s.admin_action('owner','save_destination',{'card_number':'1234567890123456','cardholder_name':'fixture','active':True})['destination']
        order=self.order(method='card')['order']
        self.s.admin_action('owner','save_destination',{'id':first['id'],'cardholder_name':'changed'})
        again=self.action(self.a,'order',{'id':order['id']})['order']
        self.assertEqual(again['payment_destination']['cardholder_name'],'fixture')
        self.assertEqual(len(self.s.admin_action('owner','destination_audit',{'id':first['id']})['audit']),2)

    def test_manual_reject_requires_reason_and_never_provisions(self):
        self.s.admin_action('owner','save_destination',{'card_number':'1234567890123456','cardholder_name':'fixture','active':True})
        payment=self.action(self.a,'topup',{'amount_toman':100000})['payment']
        self.action(self.a,'submit_receipt',{'payment_id':payment['id'],'transaction_text':'reference'},'submit-abcdefg-12345')
        with self.assertRaises(DomainError):self.s.admin_action('owner','review_payment',{'id':payment['id'],'decision':'rejected'},'review-abcdefg-12345')
        self.s.admin_action('owner','review_payment',{'id':payment['id'],'decision':'rejected','reason':'اطلاعات ناقص'},'review-abcdefg-12345')
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],0)
        self.s.process_jobs();self.assertEqual(self.native.calls,0)

    def test_referral_only_first_completed_purchase_and_refund_reversal(self):
        self.s.admin_action('owner','save_settings',{'referral_percent':20})
        code=self.action(self.a,'referrals')['referral_code']
        child=self.s.customer_from_telegram({'id':103,'first_name':'child'},code)['id']
        self.funded(child)
        first=self.order(child)['order'];self.s.process_jobs();self.s.process_jobs()
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],20000)
        self.s.admin_action('owner','refund_order',{'id':first['id'],'reason':'آزمایش'},'refund-abcdefg-12345')
        self.s.admin_action('owner','refund_order',{'id':first['id'],'reason':'آزمایش'},'refund-abcdefg-12345')
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],0)
        self.assertEqual(self.action(child,'wallet')['balance_toman'],500000)

    def test_uncertain_provisioning_not_blindly_retried(self):
        self.funded(self.a);self.order()
        def uncertain(operation,spec):self.native.calls+=1;raise RuntimeError('secret')
        self.native.provision=uncertain
        self.s.process_jobs();self.s.process_jobs();self.assertEqual(self.native.calls,1)
        jobs=self.s.admin_action('owner','jobs')['jobs'];self.assertEqual(jobs[0]['status'],'uncertain')
        self.assertNotIn('secret',str(jobs))
        with self.assertRaises(DomainError):self.s.admin_action('owner','reconcile',{'id':jobs[0]['id']})
        self.native.created[jobs[0]['operation_id']]={'id':700,'peers':[]}
        self.s.admin_action('owner','reconcile',{'id':jobs[0]['id']})
        self.assertEqual(self.action(self.a,'subscriptions')['subscriptions'][0]['native_id'],700)

    def test_discount_limit_is_transactional_and_partial_edit(self):
        discount=self.s.admin_action('owner','save_discount',{'code':'LIMITED','kind':'fixed','value':10000,'active':True,'max_uses':1})['discount']
        self.s.admin_action('owner','save_discount',{'id':discount['id'],'value':5000})
        self.funded(self.a);self.funded(self.b)
        qa=self.quote(self.a,discount_code='LIMITED');qb=self.quote(self.b,discount_code='LIMITED')
        self.action(self.a,'orders',{'quote_id':qa['id'],'payment_method':'wallet'},'first-discount-12345')
        with self.assertRaises(DomainError):self.action(self.b,'orders',{'quote_id':qb['id'],'payment_method':'wallet'},'second-discount-12345')

    def test_expiry_and_unavailable_usage(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        self.native.subscription=lambda native_id:{'id':native_id,'status':'expired','used_bytes':100,'remaining_bytes':0,'expires_at_ts':1,'remaining_seconds':0,'peers':[]}
        self.s.refresh_subscriptions();item=self.action(self.a,'subscriptions')['subscriptions'][0]
        self.assertEqual(item['status'],'expired');self.assertEqual(item['remaining_seconds'],0)
        def unavailable(native_id):raise RuntimeError('unavailable')
        self.native.subscription=unavailable;self.s.refresh_subscriptions()
        item=self.action(self.a,'subscriptions')['subscriptions'][0]
        self.assertFalse(item['usage_available']);self.assertIsNone(item['used_bytes'])

    def test_notifications_claim_retry_and_ack(self):
        first=self.s.claim_notifications();self.assertEqual(len(first),4)
        self.assertEqual(self.s.claim_notifications(),[])
        self.s.finish_notification(first[0]['id'],True)
        for item in first[1:]:self.s.finish_notification(item['id'],False,retry_after=30)
        self.assertEqual(self.s.claim_notifications(),[])

    def test_customer_support_reply_idempotent(self):
        ticket=self.action(self.a,'create_ticket',{'category':'other','body':'first'})['ticket']
        self.action(self.a,'reply_ticket',{'id':ticket['id'],'body':'second'},'reply-once-abcdefgh')
        self.action(self.a,'reply_ticket',{'id':ticket['id'],'body':'second'},'reply-once-abcdefgh')
        self.assertEqual(len(self.action(self.a,'ticket_messages',{'id':ticket['id']})['messages']),2)

    def test_config_identity_not_native_id_only(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        item=self.action(self.a,'subscriptions')['subscriptions'][0]
        with self.assertRaises(DomainError):self.action(self.a,'artifact',{'id':item['id'],'peer_id':1001,'kind':'config'})
        with self.assertRaises(DomainError):self.action(self.b,'artifact',{'id':item['id'],'peer_id':1001,'kind':'config'})

    def test_refund_requires_verified_suspension(self):
        self.funded(self.a);order=self.order()['order'];self.s.process_jobs()
        def uncertain(*a):raise RuntimeError('secret')
        self.native.suspend=uncertain
        with self.assertRaises(DomainError):self.s.admin_action('owner','refund_order',{'id':order['id'],'reason':'refund'},'refund-uncertain-12345')
        self.assertEqual(self.action(self.a,'wallet')['balance_toman'],400000)
        self.assertEqual(self.action(self.a,'order',{'id':order['id']})['order']['status'],'active')

    def test_announcement_is_durable_idempotent_and_public_plain_text(self):
        value={'title':'<title>','body':'<script>alert(1)</script>'}
        first=self.s.admin_action('owner','announce',value,'announcement-key-1234')
        second=self.s.admin_action('owner','announce',value,'announcement-key-1234')
        self.assertEqual(first,second);self.assertEqual(first['queued'],2)
        self.assertEqual(self.action(self.a,'dashboard')['notices'][0]['body'],value['body'])
        with self.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM notification_jobs WHERE kind='announcement'").fetchone()[0],2)

    def test_target_whitelist_and_public_catalog(self):
        with self.assertRaises(DomainError):self.s.admin_action('owner','save_product',{'id':self.product['id'],'target':{'scope':'local','iface_id':1,'api_key':'forbidden'}})
        with self.assertRaises(DomainError):self.s.admin_action('owner','save_settings',{'trial_target':{'scope':'node','node_id':1,'iface':'../bad'}})
        self.assertNotIn('target',self.action(self.a,'products')['products'][0])
        self.assertNotIn('target',self.quote())

    def test_actual_native_iso_and_timestamp_fields_are_preserved(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        expiry=self.s.now()+3600
        iso=datetime.fromtimestamp(expiry,timezone.utc).isoformat()
        self.native.subscription=lambda native_id:{'id':native_id,'status':'active','used_bytes':100,'remaining_bytes':1000,'expires_at':iso,'expires_at_ts':expiry,'remaining_seconds':3600,'peers':[]}
        self.s.refresh_subscriptions()
        item=self.action(self.a,'subscriptions')['subscriptions'][0]
        self.assertEqual(item['expires_at'],iso);self.assertEqual(item['expires_at_ts'],expiry)
        self.assertEqual(item['remaining_seconds'],3600)
        with self.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM notification_jobs WHERE kind='subscription_near_expiry'").fetchone()[0],1)

    def test_iso_only_expiry_fallback(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        expiry=self.s.now()+3600
        iso=datetime.fromtimestamp(expiry,timezone.utc).isoformat().replace('+00:00','Z')
        self.native.subscription=lambda native_id:{'id':native_id,'status':'active','used_bytes':100,'remaining_bytes':1000,'expires_at':iso,'remaining_seconds':3600,'peers':[]}
        self.s.refresh_subscriptions()
        with self.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM notification_jobs WHERE kind='subscription_near_expiry'").fetchone()[0],1)

    def test_upgrade_does_not_sell_unsupported_device_or_location_change(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        sid=self.action(self.a,'subscriptions')['subscriptions'][0]['id']
        product=self.s.admin_action('owner','save_product',{'name':'دو دستگاه','category':'economy','traffic_gb':20,'days':30,'devices':2,'price_toman':200000,'active':True,'target':{'scope':'local','iface_id':1}})['product']
        with self.assertRaises(DomainError):self.action(self.a,'upgrade_quote',{'id':sid,'product_id':product['id']})

    def test_bot_and_trial_interfaces_are_independent_and_strict(self):
        self.assertEqual(self.s.admin_action('owner','settings')['settings']['bot_target'],{})
        sales={'scope':'local','iface_id':2};trial={'scope':'node','node_id':8,'iface':'wg_trial'}
        self.s.admin_action('owner','save_settings',{'bot_target':sales,'trial_target':trial,'trial_enabled':True})
        quote=self.quote();claim=self.action(self.a,'trial',{},'trial-interface-key-1234')['subscription']
        with self.store.db() as db:
            paid=json.loads(db.execute('SELECT snapshot FROM quotes WHERE id=?',(quote['id'],)).fetchone()[0])
            free=json.loads(db.execute('SELECT snapshot FROM subscriptions WHERE id=?',(claim['id'],)).fetchone()[0])
        self.assertEqual(paid['target'],sales);self.assertEqual(free['target'],trial)
        for invalid in ([],{'scope':'local','iface_id':'2'},{'scope':'local','iface_id':True},{'scope':'local','iface_id':2,'api_key':'not-allowed'}):
            with self.assertRaises(DomainError):self.s.admin_action('owner','save_settings',{'bot_target':invalid})

    def test_bot_interface_change_preserves_existing_quote_and_order(self):
        self.funded(self.a)
        self.s.admin_action('owner','save_settings',{'bot_target':{'scope':'local','iface_id':2}})
        quote=self.quote()
        self.s.admin_action('owner','save_settings',{'bot_target':{'scope':'local','iface_id':3}})
        created=self.action(self.a,'create_order',{'quote_id':quote['id'],'payment_method':'wallet'},'sticky-interface-order-key')['order']
        with self.store.db() as db:
            stored=json.loads(db.execute('SELECT snapshot FROM orders WHERE id=?',(created['id'],)).fetchone()[0])
            job=json.loads(db.execute('SELECT spec FROM provisioning_jobs WHERE order_id=?',(created['id'],)).fetchone()[0])
        self.assertEqual(stored['target'],{'scope':'local','iface_id':2});self.assertEqual(job['target'],stored['target'])
        fresh=self.quote()
        with self.store.db() as db:new=json.loads(db.execute('SELECT snapshot FROM quotes WHERE id=?',(fresh['id'],)).fetchone()[0])
        self.assertEqual(new['target'],{'scope':'local','iface_id':3})

    def test_trial_activation_requires_its_own_interface_and_rolls_back(self):
        with self.assertRaises(DomainError) as caught:
            self.s.admin_action('owner','save_settings',{'bot_target':{'scope':'local','iface_id':2},'trial_enabled':True})
        self.assertEqual(caught.exception.code,'trial_unconfigured')
        settings=self.s.admin_action('owner','settings')['settings']
        self.assertEqual(settings['bot_target'],{});self.assertFalse(settings['trial_enabled'])

    def test_product_can_use_bot_interface_without_own_target(self):
        self.s.admin_action('owner','save_settings',{'bot_target':{'scope':'local','iface_id':2}})
        product=self.s.admin_action('owner','save_product',{'name':'طرح بات','category':'economy','traffic_gb':20,'days':30,'devices':1,'price_toman':200000,'active':True})['product']
        self.assertEqual(product['target'],{})
        quote=self.action(self.a,'quote',{'product_id':product['id']})['quote']
        with self.store.db() as db:snapshot=json.loads(db.execute('SELECT snapshot FROM quotes WHERE id=?',(quote['id'],)).fetchone()[0])
        self.assertEqual(snapshot['target'],{'scope':'local','iface_id':2})
        self.s.admin_action('owner','save_settings',{'bot_target':{}})
        with self.assertRaises(DomainError):self.action(self.a,'quote',{'product_id':product['id']})
        fallback=self.quote()
        with self.store.db() as db:snapshot=json.loads(db.execute('SELECT snapshot FROM quotes WHERE id=?',(fallback['id'],)).fetchone()[0])
        self.assertEqual(snapshot['target'],{'scope':'local','iface_id':1})

    def test_renewal_keeps_subscription_interface_after_bot_setting_changes(self):
        self.funded(self.a);self.order();self.s.process_jobs()
        subscription=self.action(self.a,'subscriptions')['subscriptions'][0]
        self.s.admin_action('owner','save_settings',{'bot_target':{'scope':'node','node_id':4,'iface':'wg_sales'}})
        for action in ('renew_quote','upgrade_quote'):
            quote=self.action(self.a,action,{'id':subscription['id'],'product_id':self.product['id']})['quote']
            with self.store.db() as db:snapshot=json.loads(db.execute('SELECT snapshot FROM quotes WHERE id=?',(quote['id'],)).fetchone()[0])
            self.assertEqual(snapshot['target'],{'scope':'local','iface_id':1})
        with self.assertRaises(DomainError):self.quote(target={'scope':'local','iface_id':9})

if __name__ == '__main__':unittest.main()
