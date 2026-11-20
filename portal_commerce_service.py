"""Transactional Telegram commerce domain; native WG Portal remains provisioning authority.

Amounts are integer toman. One toman is ten Iranian rial. No prices, bank
destinations or promotions are enabled implicitly.
"""
import hashlib
from collections import Counter
from datetime import datetime, timezone
import json
import re
import secrets
import sqlite3
import time


class DomainError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def fail(code='invalid_request', message='اطلاعات درخواست معتبر نیست.', status=400):
    raise DomainError(code, message, status)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def integer(value, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        fail()
    return value


def text(value, maximum=2000, required=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        fail()
    value = value.strip()
    if required and not value:
        fail()
    return value


def target_spec(value, allow_empty=False):
    if not isinstance(value,dict):fail()
    if allow_empty and not value:return {}
    if value.get('scope')=='local' and set(value)=={'scope','iface_id'}:
        return {'scope':'local','iface_id':integer(value['iface_id'],1)}
    if value.get('scope')=='node' and set(value)=={'scope','node_id','iface'}:
        iface=text(value['iface'],15)
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}',iface):fail()
        return {'scope':'node','node_id':integer(value['node_id'],1),'iface':iface}
    fail('invalid_target','مقصد WireGuard باید اینترفیس محلی یا سرور و اینترفیس مشخص باشد.')


class CommerceService:
    DEFAULTS = {'currency': 'toman', 'rial_per_toman': 10, 'trial_enabled': False,
                'bot_target': {},
                'trial_hours': 24, 'trial_traffic_gb': 1, 'trial_target': {},
                'trial_capacity': 100, 'referral_percent': 0, 'support_username': 'AshkanfathiOfficial',
                'extra_device_price_toman': 0, 'expiry_notice_hours': 72, 'traffic_warning_percent': 10,
                'topup_min_toman': 10000, 'topup_max_toman': 10000000,
                'topup_amounts': [100000, 250000, 500000, 1000000],
                'payment_instructions': 'پس از واریز مبلغ دقیق، تصویر رسید را ثبت کنید. اشتراک پس از تأیید مدیر فعال می‌شود.'}

    def __init__(self, store, native_adapter=None, clock=None):
        self.store, self.native = store, native_adapter
        self.clock = clock or time.time

    def now(self): return int(self.clock())

    def _audit(self, db, actor, action, target=''):
        db.execute('INSERT INTO commerce_audit(actor,action,target,created_at) VALUES(?,?,?,?)',
                   (str(actor), action, str(target), self.now()))

    def _notify(self, db, customer_id, kind, payload, unique):
        db.execute('INSERT OR IGNORE INTO notification_jobs(customer_id,kind,payload,unique_key,available_at,created_at) VALUES(?,?,?,?,?,?)',
                   (customer_id, kind, dumps(payload), unique, self.now(), self.now()))

    def _customer(self, db, customer_id, allow_suspended=False):
        row = db.execute('SELECT * FROM customers WHERE id=?', (integer(customer_id, 1),)).fetchone()
        if row is None: fail('not_found', 'حساب مشتری پیدا نشد.', 404)
        if row['suspended'] and not allow_suspended: fail('customer_suspended', 'دسترسی این حساب متوقف شده است.', 403)
        return dict(row)

    def customer_from_telegram(self, identity, referral_code=None):
        if not isinstance(identity, dict): fail()
        telegram_id = integer(identity.get('id'), 1, 2**63-1)
        first_name = text(identity.get('first_name', ''), 128, False)
        username = text(identity.get('username', ''), 64, False)
        with self.store.db(write=True) as db:
            row = db.execute('SELECT * FROM customers WHERE telegram_id=?', (telegram_id,)).fetchone()
            if row is None:
                referrer = db.execute('SELECT id FROM customers WHERE referral_code=?', (str(referral_code or '')[:80],)).fetchone()
                cid = db.execute('INSERT INTO customers(telegram_id,first_name,username,referral_code,referrer_id,created_at) VALUES(?,?,?,?,?,?)',
                    (telegram_id, first_name, username, secrets.token_urlsafe(12), referrer['id'] if referrer else None, self.now())).lastrowid
                self._audit(db, 'telegram:' + str(telegram_id), 'customer:register', cid)
                self._notify(db, cid, 'registration', {}, 'register:' + str(cid))
                self._notify(db, None, 'admin_registration', {'customer_id':cid}, 'admin-register:' + str(cid))
                row = db.execute('SELECT * FROM customers WHERE id=?', (cid,)).fetchone()
            else:
                db.execute('UPDATE customers SET first_name=?,username=? WHERE id=?', (first_name, username, row['id']))
                row = db.execute('SELECT * FROM customers WHERE id=?', (row['id'],)).fetchone()
            return dict(row)

    customer = customer_from_telegram

    def _settings(self, db):
        settings = dict(self.DEFAULTS)
        for row in db.execute('SELECT name,value FROM commerce_settings'):
            settings[row['name']] = json.loads(row['value'])
        return settings

    def _balance(self, db, customer_id):
        return db.execute('SELECT coalesce(sum(amount_toman),0) FROM wallet_ledger WHERE customer_id=?', (customer_id,)).fetchone()[0]

    def _ledger(self, db, cid, amount, kind, reference, reason):
        if db.execute('SELECT id FROM wallet_ledger WHERE reference=?', (reference,)).fetchone(): return
        if self._balance(db, cid) + amount < 0:
            fail('insufficient_balance', 'موجودی کیف پول کافی نیست.', 409)
        db.execute('INSERT INTO wallet_ledger(customer_id,amount_toman,kind,reference,reason,created_at) VALUES(?,?,?,?,?,?)',
                   (cid, amount, kind, reference, reason, self.now()))

    def _receipt(self, db, actor, request_id, payload, fn):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{16,100}', request_id):
            fail('idempotency_required', 'شناسهٔ یکتای درخواست الزامی است.')
        fingerprint = hashlib.sha256(dumps(payload).encode()).hexdigest()
        previous = db.execute('SELECT * FROM commerce_receipts WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint: fail('idempotency_conflict', 'این درخواست قبلاً با اطلاعات متفاوت ثبت شده است.', 409)
            return json.loads(previous['response'])
        response = fn()
        db.execute('INSERT INTO commerce_receipts VALUES(?,?,?,?,?)', (actor, request_id, fingerprint, dumps(response), self.now()))
        return response

    @staticmethod
    def _product(row):
        value = dict(row)
        value['active'] = bool(value['active'])
        value['target'] = json.loads(value['target'])
        value['pricing'] = json.loads(value['pricing'])
        value['unlimited'] = value['traffic_gb'] is None
        return value

    @staticmethod
    def _destination(row):
        value = dict(row)
        value['cardholder_name'] = value.pop('holder_name')
        value['active'] = bool(value['active'])
        return value

    def _destination_current(self, db):
        row = db.execute('SELECT * FROM payment_destinations WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()
        if not row: fail('payment_unconfigured', 'مدیر هنوز روش پرداخت کارت‌به‌کارت را فعال نکرده است.', 409)
        return self._destination(row)

    def _quote(self, db, cid, payload, existing_subscription=None, action='create'):
        allowed = {'product_id', 'traffic_gb', 'days', 'devices', 'discount_code'}
        if set(payload)-allowed: fail()
        pid = integer(payload.get('product_id'), 1)
        row = db.execute('SELECT * FROM products WHERE id=? AND active=1', (pid,)).fetchone()
        if not row: fail('product_unavailable', 'این محصول در حال حاضر قابل خرید نیست.', 404)
        product = self._product(row)
        configured_target=self._settings(db)['bot_target']
        selected_target=configured_target or product['target']
        traffic, days, devices = product['traffic_gb'], product['days'], product['devices']
        pricing = product['pricing']
        if 'traffic_gb' in payload:
            traffic = payload['traffic_gb']
            if traffic != product['traffic_gb']:
                if traffic not in pricing.get('traffic_options', []): fail('unsupported_selection', 'حجم انتخاب‌شده برای این محصول فعال نیست.')
                integer(traffic, 1, 1000000)
        if 'days' in payload:
            days = integer(payload['days'], 1, 3650)
            if days != product['days'] and days not in pricing.get('duration_options', []): fail('unsupported_selection', 'مدت انتخاب‌شده برای این محصول فعال نیست.')
        if 'devices' in payload:
            devices = integer(payload['devices'], 1, 5)
            if devices != product['devices'] and devices not in pricing.get('device_options', []): fail('unsupported_selection', 'تعداد کانفیگ انتخاب‌شده فعال نیست.')
        base = product['price_toman']
        if traffic != product['traffic_gb']:
            prices = pricing.get('traffic_prices', {})
            if str(traffic) not in prices: fail('price_unconfigured', 'قیمت این حجم تنظیم نشده است.', 409)
            base = integer(prices[str(traffic)], 1)
        if days != product['days']:
            multipliers = pricing.get('duration_percent', {})
            if str(days) not in multipliers: fail('price_unconfigured', 'قیمت این مدت تنظیم نشده است.', 409)
            base = base * integer(multipliers[str(days)], 1, 10000) // 100
        if devices != product['devices']:
            base += (devices-product['devices']) * integer(pricing.get('additional_device_toman', self._settings(db)['extra_device_price_toman']), 0)
        if base <= 0:fail('price_unconfigured','قیمت انتخاب‌شده معتبر نیست؛ مدیر باید قیمت محصول را اصلاح کند.',409)
        discount_toman, discount_id = 0, None
        if payload.get('discount_code'):
            code = text(payload['discount_code'], 64).upper()
            discount = db.execute('SELECT * FROM discounts WHERE code=? COLLATE NOCASE AND active=1', (code,)).fetchone()
            self._check_discount(db, discount, cid, pid)
            discount_id = discount['id']
            discount_toman = min(base, base*discount['value']//100 if discount['kind']=='percent' else discount['value'])
        snapshot = {'product_id':pid, 'product_name':product['name'], 'category':product['category'],
                    'traffic_gb':traffic, 'days':days, 'devices':devices,
                    'base_toman':base, 'discount_toman':discount_toman, 'total_toman':base-discount_toman,
                    'target':selected_target, 'discount_id':discount_id, 'action':action,
                    'existing_subscription_id':existing_subscription, 'currency':'toman'}
        if existing_subscription:
            previous=db.execute('SELECT snapshot FROM subscriptions WHERE id=? AND customer_id=?',(existing_subscription,cid)).fetchone()
            original=json.loads(previous['snapshot']) if previous else {}
            if original.get('devices')!=devices:
                fail('new_subscription_required','تمدید و ارتقا با حفظ مقصد و تعداد کانفیگ انجام می‌شود؛ برای دستگاه یا مقصد جدید اشتراک جدا بخرید.',409)
            snapshot['target']=original.get('target')
        snapshot['target']=target_spec(snapshot['target'])
        qid = secrets.token_urlsafe(24)
        expires = self.now()+900
        db.execute('INSERT INTO quotes(id,customer_id,snapshot,total_toman,expires_at) VALUES(?,?,?,?,?)', (qid,cid,dumps(snapshot),snapshot['total_toman'],expires))
        public={k:v for k,v in snapshot.items() if k not in {'target','discount_id'}}
        return {'quote':{**public, 'id':qid, 'expires_at':expires}}

    def _check_discount(self, db, discount, cid, pid):
        if not discount or not discount['active'] or discount['expires_at'] and discount['expires_at'] <= self.now(): fail('discount_invalid', 'کد تخفیف معتبر نیست.')
        products = json.loads(discount['product_ids'])
        if products and pid not in products: fail('discount_invalid', 'این کد برای محصول انتخاب‌شده نیست.')
        total = db.execute('SELECT count(*) FROM discount_redemptions WHERE discount_id=?', (discount['id'],)).fetchone()[0]
        own = db.execute('SELECT count(*) FROM discount_redemptions WHERE discount_id=? AND customer_id=?', (discount['id'],cid)).fetchone()[0]
        if discount['max_uses'] is not None and total >= discount['max_uses'] or own >= discount['per_customer']:
            fail('discount_exhausted', 'سهمیهٔ این کد تخفیف تمام شده است.', 409)
        if discount['first_purchase'] and db.execute("SELECT 1 FROM orders WHERE customer_id=? AND status IN ('paid','provisioning','active')", (cid,)).fetchone():
            fail('discount_invalid', 'این تخفیف فقط برای اولین خرید است.')

    def _order_value(self, db, row):
        value = dict(row)
        snapshot = json.loads(value.pop('snapshot'))
        value.update({k:v for k,v in snapshot.items() if k not in {'target','discount_id'}})
        if value['payment_id']:
            payment = db.execute('SELECT * FROM payments WHERE id=?', (value['payment_id'],)).fetchone()
            value['payment_destination'] = json.loads(payment['destination_snapshot'])
            value['payment_status'] = payment['status']
            value['payment_reason'] = payment['reason']
        else: value['payment_destination'] = None
        return value

    def _payment_value(self, row):
        value = dict(row)
        value['payment_destination'] = json.loads(value.pop('destination_snapshot'))
        return value

    def _new_payment(self, db, cid, amount, kind, order_id=None):
        destination = self._destination_current(db)
        pid = db.execute('INSERT INTO payments(customer_id,order_id,kind,amount_toman,status,destination_snapshot,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)',
                         (cid,order_id,kind,amount,'pending_payment',dumps(destination),self.now(),self.now()+86400)).lastrowid
        return self._payment_value(db.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone())

    def _create_order(self, db, cid, payload):
        if set(payload) != {'quote_id','payment_method'}: fail()
        qid, method = text(payload['quote_id'],100), payload['payment_method']
        if method not in {'card','wallet'}: fail()
        quote = db.execute('SELECT * FROM quotes WHERE id=? AND customer_id=?', (qid,cid)).fetchone()
        if not quote or quote['expires_at'] <= self.now(): fail('quote_expired', 'پیش‌فاکتور منقضی شده است؛ قیمت را دوباره دریافت کنید.', 409)
        if quote['consumed_order']: fail('quote_consumed', 'این پیش‌فاکتور قبلاً به سفارش تبدیل شده است.', 409)
        spec = json.loads(quote['snapshot'])
        if spec['discount_id']:
            discount = db.execute('SELECT * FROM discounts WHERE id=?', (spec['discount_id'],)).fetchone()
            self._check_discount(db,discount,cid,spec['product_id'])
        oid = db.execute('INSERT INTO orders(customer_id,quote_id,status,snapshot,total_toman,payment_method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (cid,qid,'pending_payment',quote['snapshot'],quote['total_toman'],method,self.now(),self.now())).lastrowid
        payment = None
        if method == 'wallet':
            self._ledger(db,cid,-quote['total_toman'],'purchase','order:'+str(oid),'خرید اشتراک')
            db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
            self._fulfillment_job(db,oid,cid,spec)
        else:
            payment = self._new_payment(db,cid,quote['total_toman'],'order',oid)
            db.execute('UPDATE orders SET payment_id=? WHERE id=?', (payment['id'],oid))
        if spec['discount_id']:
            db.execute('INSERT INTO discount_redemptions(discount_id,customer_id,order_id,created_at) VALUES(?,?,?,?)', (spec['discount_id'],cid,oid,self.now()))
        db.execute('UPDATE quotes SET consumed_order=? WHERE id=?', (oid,qid))
        self._audit(db,'customer:'+str(cid),'order:create',oid)
        return {'order':self._order_value(db,db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()),'payment':payment}

    def _fulfillment_job(self, db, oid, cid, spec, trial=False):
        if oid and db.execute('SELECT 1 FROM provisioning_jobs WHERE order_id=?', (oid,)).fetchone(): return
        existing = spec.get('existing_subscription_id')
        if existing:
            row = db.execute('SELECT * FROM subscriptions WHERE id=? AND customer_id=? AND native_id IS NOT NULL', (existing,cid)).fetchone()
            if not row: fail('not_found','اشتراک پیدا نشد.',404)
            if row['status']=='refund_pending':fail('refund_pending','این اشتراک در حال بررسی بازپرداخت است.',409)
            sid = existing; spec = {**spec,'existing_native_id':row['native_id']}
        else:
            sid = db.execute('INSERT INTO subscriptions(customer_id,order_id,status,snapshot,trial,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                             (cid,oid,'pending',dumps(spec),int(trial),self.now(),self.now())).lastrowid
        spec = {**spec,'order_id':oid,'customer_id':cid}
        operation = 'commerce-order-'+str(oid) if oid else 'commerce-trial-'+str(cid)
        db.execute('INSERT INTO provisioning_jobs(operation_id,order_id,subscription_id,status,spec,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (operation,oid,sid,'pending',dumps(spec),self.now(),self.now()))
        return sid

    def _subscription(self, row):
        item = dict(row); snapshot = json.loads(item.pop('snapshot')); native = json.loads(item.pop('native_snapshot') or '{}')
        item.update({k:snapshot.get(k) for k in ('product_name','traffic_gb','days','devices')})
        item['peers'] = [{k:p[k] for k in ('id','name','public_key') if k in p} for p in native.get('peers',[]) if isinstance(p,dict)]
        item['used_bytes'] = native.get('used_bytes')
        item['remaining_bytes'] = native.get('remaining_bytes')
        item['expires_at'] = native.get('expires_at')
        item['expires_at_ts'] = native.get('expires_at_ts')
        item['remaining_seconds'] = native.get('remaining_seconds')
        item['limit_bytes'] = native.get('limit_bytes')
        item['usage_available'] = native.get('available',True) and item['used_bytes'] is not None
        if not item['usage_available']:
            item['used_bytes']=None;item['remaining_bytes']=None;item['remaining_seconds']=None
        return item

    def _native_time_fields(self, snapshot):
        """Keep native display ISO and numeric time without local-zone ambiguity."""
        expiry=snapshot.get('expires_at_ts')
        if type(expiry) not in {int,float}:
            expiry=snapshot.get('expires_at')
            if isinstance(expiry,str):
                try:
                    stamp=datetime.fromisoformat(expiry.replace('Z','+00:00'))
                    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
                    expiry=stamp.timestamp()
                except (ValueError,OverflowError):expiry=None
        if type(expiry) in {int,float}:
            try:snapshot['expires_at_ts']=int(expiry)
            except (ValueError,OverflowError):snapshot['expires_at_ts']=None
        if snapshot.get('remaining_seconds') is None and snapshot.get('expires_at_ts') is not None:
            snapshot['remaining_seconds']=max(0,snapshot['expires_at_ts']-self.now())
        return snapshot

    def customer_action(self, customer_id, action, payload=None, request_id=None):
        if not isinstance(payload or {},dict): fail()
        payload = payload or {}
        if action == 'create_order': action = 'orders'
        if action == 'artifact': return self._artifact(customer_id,payload)
        with self.store.db(write=True) as db:
            customer = self._customer(db,customer_id)
            cid = customer['id']
            if action == 'dashboard':
                subs = [self._subscription(row) for row in db.execute('SELECT * FROM subscriptions WHERE customer_id=? ORDER BY id DESC',(cid,))]
                active = [s for s in subs if s['status']=='active']
                remaining=[s['remaining_seconds'] for s in active if isinstance(s.get('remaining_seconds'),(int,float))]
                return {'customer':customer,'wallet_balance_toman':self._balance(db,cid),'active_subscriptions':len(active),
                        'remaining_days':(max(0,int(min(remaining)+86399)//86400) if remaining and len(remaining)==len(active) else None),
                        'remaining_bytes':sum(s['remaining_bytes'] for s in active) if active and all(s['remaining_bytes'] is not None for s in active) else None,
                        'subscriptions':subs,'notices':[dict(row) for row in db.execute('SELECT id,title,body,created_at FROM customer_announcements WHERE active=1 ORDER BY id DESC LIMIT 30')],
                        'trial_available':bool(self._settings(db)['trial_enabled']) and not db.execute('SELECT 1 FROM trial_claims WHERE customer_id=?',(cid,)).fetchone()}
            if action == 'products': return {'products':[{k:v for k,v in self._product(row).items() if k!='target'} for row in db.execute('SELECT * FROM products WHERE active=1 ORDER BY id')]}
            if action == 'quote': return self._quote(db,cid,payload)
            if action in {'renew_quote','upgrade_quote'}:
                sid = integer(payload.get('id'),1)
                sub = db.execute('SELECT * FROM subscriptions WHERE id=? AND customer_id=?',(sid,cid)).fetchone()
                if not sub: fail('not_found','اشتراک پیدا نشد.',404)
                if sub['status']=='refund_pending':fail('refund_pending','این اشتراک در حال بررسی بازپرداخت است.',409)
                base = json.loads(sub['snapshot']); selection = {k:v for k,v in payload.items() if k!='id'}
                selection.setdefault('product_id',base['product_id'])
                return self._quote(db,cid,selection,sid,'renew' if action=='renew_quote' else 'upgrade')
            if action == 'orders':
                if payload: return self._receipt(db,'customer:'+str(cid),request_id,{'action':action,**payload},lambda:self._create_order(db,cid,payload))
                return {'orders':[self._order_value(db,row) for row in db.execute('SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC',(cid,))]}
            if action == 'order':
                row = db.execute('SELECT * FROM orders WHERE id=? AND customer_id=?',(integer(payload.get('id'),1),cid)).fetchone()
                if not row: fail('not_found','سفارش پیدا نشد.',404)
                result = {'order':self._order_value(db,row)}
                if row['payment_id']: result['payment']=self._payment_value(db.execute('SELECT * FROM payments WHERE id=?',(row['payment_id'],)).fetchone())
                return result
            if action == 'payment':
                row=db.execute('SELECT * FROM payments WHERE id=? AND customer_id=?',(integer(payload.get('id'),1),cid)).fetchone()
                if not row: fail('not_found','پرداخت پیدا نشد.',404)
                return {'payment':self._payment_value(row)}
            if action == 'receipt_payments':
                rows=db.execute("""SELECT id,order_id,amount_toman FROM payments
                    WHERE customer_id=? AND status IN ('pending_payment','pending_review') AND expires_at>?
                    ORDER BY id DESC LIMIT 41""",(cid,self.now()))
                return {'payments':[dict(row) for row in rows]}
            if action == 'cancel_order':
                oid=integer(payload.get('id'),1);row=db.execute('SELECT * FROM orders WHERE id=? AND customer_id=?',(oid,cid)).fetchone()
                if not row:fail('not_found','سفارش پیدا نشد.',404)
                if row['status'] not in {'pending_payment','cancelled'}:fail('order_not_cancellable','این سفارش قابل لغو نیست.',409)
                db.execute("UPDATE orders SET status='cancelled',updated_at=? WHERE id=?",(self.now(),oid))
                db.execute("UPDATE payments SET status='cancelled' WHERE order_id=? AND status='pending_payment'",(oid,))
                db.execute('DELETE FROM discount_redemptions WHERE order_id=?',(oid,))
                self._audit(db,'customer:'+str(cid),'order:cancel',oid)
                return {'order':self._order_value(db,db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone())}
            if action == 'submit_receipt':
                return self._receipt(db,'customer:'+str(cid),request_id,{'action':action,**payload},lambda:self._submit_receipt(db,cid,payload))
            if action == 'wallet': return {'balance_toman':self._balance(db,cid),'currency':'toman','transactions':[dict(row) for row in db.execute('SELECT * FROM wallet_ledger WHERE customer_id=? ORDER BY id DESC',(cid,))],'topup_amounts':self._settings(db)['topup_amounts']}
            if action == 'topup':
                settings=self._settings(db);amount=integer(payload.get('amount_toman'),settings['topup_min_toman'],settings['topup_max_toman'])
                return self._receipt(db,'customer:'+str(cid),request_id,{'action':action,'amount_toman':amount},lambda:{'payment':self._new_payment(db,cid,amount,'topup')})
            if action == 'subscriptions': return {'subscriptions':[self._subscription(row) for row in db.execute('SELECT * FROM subscriptions WHERE customer_id=? ORDER BY id DESC',(cid,))]}
            if action == 'subscription':
                row=db.execute('SELECT * FROM subscriptions WHERE id=? AND customer_id=?',(integer(payload.get('id'),1),cid)).fetchone()
                if not row: fail('not_found','اشتراک پیدا نشد.',404)
                return {'subscription':self._subscription(row)}
            if action == 'trial':
                return self._receipt(db,'customer:'+str(cid),request_id,{'action':'trial'},lambda:self._trial(db,cid))
            if action == 'referrals':
                referred=db.execute('SELECT count(*) FROM customers WHERE referrer_id=?',(cid,)).fetchone()[0]
                rewards=[dict(row) for row in db.execute('SELECT * FROM referral_rewards WHERE referrer_id=? ORDER BY created_at DESC',(cid,))]
                return {'referral_code':customer['referral_code'],'referral_count':referred,'successful_purchases':len(rewards),'earnings_toman':sum(x['amount_toman'] for x in rewards),'rewards':rewards,'percent':self._settings(db)['referral_percent']}
            if action == 'tickets': return {'tickets':[dict(row) for row in db.execute('SELECT * FROM support_tickets WHERE customer_id=? ORDER BY id DESC',(cid,))]}
            if action == 'create_ticket': return self._receipt(db,'customer:'+str(cid),request_id,{'action':action,**payload},lambda:self._create_ticket(db,cid,payload))
            if action in {'ticket_messages','reply_ticket'}:
                tid=integer(payload.get('id'),1);ticket=db.execute('SELECT * FROM support_tickets WHERE id=? AND customer_id=?',(tid,cid)).fetchone()
                if not ticket:fail('not_found','تیکت پیدا نشد.',404)
                if action=='reply_ticket':
                    return self._receipt(db,'customer:'+str(cid),request_id,{'action':action,**payload},lambda:self._reply_result(db,tid,'customer:'+str(cid),payload))
                return {'ticket':dict(ticket),'messages':[dict(row) for row in db.execute('SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id',(tid,))]}
            if action == 'ticket_attachment':
                row=db.execute('SELECT m.attachment_reference FROM support_messages m JOIN support_tickets t ON t.id=m.ticket_id WHERE t.customer_id=? AND t.id=? AND m.id=?',(cid,integer(payload.get('id'),1),integer(payload.get('attachment_id'),1))).fetchone()
                if not row or not row['attachment_reference']:fail('not_found','پیوست پیدا نشد.',404)
                return {'attachment_reference':row['attachment_reference']}
            fail('unknown_action','این عملیات پشتیبانی نمی‌شود.',404)

    def _artifact(self,cid,payload):
        sid=integer(payload.get('id'),1);pid=integer(payload.get('peer_id'),1);kind=payload.get('kind')
        if kind not in {'config','qr'}:fail()
        with self.store.db() as db:
            self._customer(db,cid)
            row=db.execute('SELECT * FROM subscriptions WHERE id=? AND customer_id=?',(sid,cid)).fetchone()
            if not row or row['native_id'] is None:fail('not_found','اشتراک آماده نیست.',404)
            original=json.loads(row['native_snapshot'] or '{}')
            peer=next((p for p in original.get('peers',[]) if p.get('id')==pid),None)
            if not peer or not peer.get('public_key'):fail('not_found','کانفیگ متعلق به این اشتراک نیست.',404)
        if self.native is None:fail('native_unavailable','سرویس بومی در دسترس نیست.',503)
        try:
            live=self.native.subscription(row['native_id'])
            current=next((p for p in live.get('peers',[]) if p.get('id')==pid),None)
            if not current or current.get('public_key')!=peer['public_key']:fail('ownership_changed','هویت کانفیگ تغییر کرده است؛ با پشتیبانی تماس بگیرید.',409)
            result=self.native.artifact(row['native_id'],pid,kind)
        except DomainError:raise
        except Exception:fail('artifact_unavailable','دریافت کانفیگ ممکن نشد؛ دوباره تلاش کنید.',503)
        if isinstance(result,tuple) and len(result)==2:
            body,mime=result
        elif isinstance(result,dict):body,mime=result.get('body'),result.get('content_type',result.get('mime'))
        else:fail('artifact_unavailable','پاسخ کانفیگ معتبر نیست.',503)
        if not isinstance(body,bytes):fail('artifact_unavailable','پاسخ کانفیگ معتبر نیست.',503)
        return {'body':body,'content_type':mime or ('image/png' if kind=='qr' else 'text/plain; charset=utf-8'),'mime':mime or ('image/png' if kind=='qr' else 'text/plain; charset=utf-8')}

    def _submit_receipt(self,db,cid,payload):
        pid=integer(payload.get('payment_id',payload.get('id')),1)
        row=db.execute('SELECT * FROM payments WHERE id=? AND customer_id=?',(pid,cid)).fetchone()
        if not row:fail('not_found','پرداخت پیدا نشد.',404)
        if row['expires_at'] <= self.now():fail('payment_expired','مهلت پرداخت تمام شده است.',409)
        if row['status'] not in {'pending_payment','pending_review'}:fail('payment_not_pending','این پرداخت قابل ویرایش نیست.',409)
        reference=text(payload.get('receipt_reference',''),200,False)
        transaction=text(payload.get('transaction_text',''),1000,False)
        if reference and not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',reference):fail()
        if not reference and not transaction:fail('receipt_required','رسید یا اطلاعات تراکنش را ثبت کنید.')
        db.execute("UPDATE payments SET status='pending_review',receipt_reference=?,transaction_text=? WHERE id=?",
                   (reference or row['receipt_reference'],transaction or row['transaction_text'] or '',pid))
        self._notify(db,cid,'payment_submitted',{'payment_id':pid},'payment-submitted:'+str(pid))
        self._notify(db,None,'admin_payment_submitted',{'payment_id':pid,'customer_id':cid,'amount_toman':row['amount_toman']},'admin-payment-submitted:'+str(pid))
        self._audit(db,'customer:'+str(cid),'payment:submit',pid)
        return {'payment':self._payment_value(db.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone())}

    def _trial(self,db,cid):
        settings=self._settings(db)
        if not settings['trial_enabled']:fail('trial_disabled','تست رایگان فعلاً فعال نیست.',409)
        if db.execute('SELECT 1 FROM trial_claims WHERE customer_id=?',(cid,)).fetchone():fail('trial_already_used','هر مشتری فقط یک بار می‌تواند تست بگیرد.',409)
        if not settings['trial_target']:fail('trial_unconfigured','مقصد تست تنظیم نشده است.',409)
        count=db.execute("SELECT count(*) FROM subscriptions WHERE trial=1 AND status IN ('pending','provisioning','active')").fetchone()[0]
        if count>=settings['trial_capacity']:fail('trial_capacity','ظرفیت تست تکمیل شده است.',409)
        spec={'product_name':'تست رایگان','product_id':None,'traffic_gb':settings['trial_traffic_gb'],'days':settings['trial_hours']/24,'devices':1,'target':settings['trial_target'],'action':'create','total_toman':0}
        sid=self._fulfillment_job(db,None,cid,spec,True)
        db.execute('INSERT INTO trial_claims VALUES(?,?,?)',(cid,sid,self.now()))
        self._audit(db,'customer:'+str(cid),'trial:claim',sid)
        return {'subscription':self._subscription(db.execute('SELECT * FROM subscriptions WHERE id=?',(sid,)).fetchone())}

    def _create_ticket(self,db,cid,payload):
        category=payload.get('category')
        if category not in {'payment','connection','subscription','wallet','other'}:fail()
        body=text(payload.get('body'),4000)
        tid=db.execute("INSERT INTO support_tickets(customer_id,category,status,created_at,updated_at) VALUES(?,?,'open',?,?)",(cid,category,self.now(),self.now())).lastrowid
        self._reply(db,tid,'customer:'+str(cid),{'body':body,'attachment_reference':payload.get('attachment_reference')})
        return {'ticket':dict(db.execute('SELECT * FROM support_tickets WHERE id=?',(tid,)).fetchone())}

    @staticmethod
    def _support_receipts(db,customer_id=None):
        customer_filter='AND customer_id=?' if customer_id is not None else ''
        params=(customer_id,) if customer_id is not None else ()
        return [dict(row) for row in db.execute(f"""
            SELECT id,customer_id,order_id,amount_toman,status,created_at FROM payments
            WHERE length(receipt_reference)=52 AND substr(receipt_reference,-4)='.png'
                AND substr(receipt_reference,1,48) NOT GLOB '*[^0-9a-f]*'
                {customer_filter}
            ORDER BY id DESC
        """,params)]

    def _reply(self,db,tid,actor,payload):
        body=text(payload.get('body'),4000)
        attachment=payload.get('attachment_reference')
        if attachment and (not isinstance(attachment,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',attachment)):fail()
        mid=db.execute('INSERT INTO support_messages(ticket_id,actor,body,attachment_reference,created_at) VALUES(?,?,?,?,?)',(tid,actor,body,attachment,self.now())).lastrowid
        db.execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?",('answered' if actor.startswith('admin:') else 'open',self.now(),tid))
        row=db.execute('SELECT customer_id FROM support_tickets WHERE id=?',(tid,)).fetchone()
        if actor.startswith('admin:'):self._notify(db,row['customer_id'],'support_reply',{'ticket_id':tid,'body':body},'support:'+str(mid))

    def _reply_result(self,db,tid,actor,payload):
        self._reply(db,tid,actor,payload)
        if actor.startswith('admin:') and 'close' in payload:
            if type(payload['close']) is not bool:fail()
            if payload['close']:db.execute("UPDATE support_tickets SET status='closed',updated_at=? WHERE id=?",(self.now(),tid))
        return {'ticket':dict(db.execute('SELECT * FROM support_tickets WHERE id=?',(tid,)).fetchone()),'messages':[dict(row) for row in db.execute('SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id',(tid,))]}

    def save_settings_atomically(self,actor,payload,runtime_writer=None):
        """Commit domain settings and validated bot configuration together."""
        actor=text(str(actor),200)
        with self.store.db(write=True) as db:
            result=self._save_settings(db,actor,payload) if payload else {'settings':self._settings(db)}
            if runtime_writer is not None:runtime_writer(db)
            return result

    def admin_action(self, actor, action, payload=None, request_id=None):
        actor=text(str(actor),200);payload=payload or {}
        if not isinstance(payload,dict):fail()
        if action=='refund_order':return self._refund_external(actor,payload,request_id)
        with self.store.db(write=True) as db:
            if action=='dashboard':
                totals={name:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for name,table in [('total_customers','customers'),('total_orders','orders')]}
                totals.update(active_subscriptions=db.execute("SELECT count(*) FROM subscriptions WHERE status='active'").fetchone()[0],trial_subscriptions=db.execute('SELECT count(*) FROM subscriptions WHERE trial=1').fetchone()[0],pending_payments=db.execute("SELECT count(*) FROM payments WHERE status='pending_review'").fetchone()[0],failed_provisioning=db.execute("SELECT count(*) FROM provisioning_jobs WHERE status IN ('uncertain','failed')").fetchone()[0],wallet_liabilities_toman=db.execute('SELECT coalesce(sum(amount_toman),0) FROM wallet_ledger').fetchone()[0],revenue_toman=db.execute("SELECT coalesce(sum(total_toman),0) FROM orders WHERE status='active'").fetchone()[0],referral_rewards_toman=db.execute('SELECT coalesce(sum(amount_toman),0) FROM referral_rewards').fetchone()[0])
                return {'dashboard':totals,**totals}
            if action=='products':return {'products':[self._product(row) for row in db.execute('SELECT * FROM products ORDER BY id DESC')]}
            if action=='save_product':return self._save_product(db,actor,payload)
            if action=='destinations':return {'destinations':[self._destination(row) for row in db.execute('SELECT * FROM payment_destinations ORDER BY id DESC')]}
            if action=='save_destination':return self._save_destination(db,actor,payload)
            if action=='destination_audit':return {'audit':[dict(row) for row in db.execute('SELECT * FROM destination_audit WHERE destination_id=? ORDER BY id DESC',(integer(payload.get('id'),1),))]}
            if action=='payments':
                status=payload.get('status','all')
                if status not in {'all','pending_payment','pending_review','approved','rejected','expired','cancelled'}:fail()
                rows=db.execute('SELECT * FROM payments WHERE status=? ORDER BY id DESC',(status,)) if status!='all' else db.execute('SELECT * FROM payments ORDER BY id DESC')
                return {'payments':[self._payment_value(row) for row in rows]}
            if action=='payment':
                row=db.execute('SELECT * FROM payments WHERE id=?',(integer(payload.get('id'),1),)).fetchone()
                if not row:fail('not_found','پرداخت پیدا نشد.',404)
                return {'payment':self._payment_value(row)}
            if action=='review_payment':return self._receipt(db,'admin:'+actor,request_id,{'action':action,**payload},lambda:self._review_payment(db,actor,payload))
            if action=='customers':return {'customers':[dict(row) for row in db.execute('SELECT * FROM customers ORDER BY id DESC')]}
            if action=='customer':
                cid=integer(payload.get('id'),1);customer=self._customer(db,cid,True)
                return {'customer':customer,'subscriptions':[self._subscription(row) for row in db.execute('SELECT * FROM subscriptions WHERE customer_id=?',(cid,))],'transactions':[dict(row) for row in db.execute('SELECT * FROM wallet_ledger WHERE customer_id=? ORDER BY id DESC',(cid,))]}
            if action=='suspend_customer':
                cid=integer(payload.get('id'),1);self._customer(db,cid,True)
                if type(payload.get('suspended')) is not bool:fail()
                db.execute('UPDATE customers SET suspended=? WHERE id=?',(int(payload['suspended']),cid));self._audit(db,actor,'customer:suspend:'+str(payload['suspended']),cid)
                return {'customer':self._customer(db,cid,True)}
            if action=='wallet_adjustment':
                cid=integer(payload.get('id'),1);self._customer(db,cid,True)
                amount=integer(payload.get('amount_toman'),-10**10,10**10);reason=text(payload.get('reason'),500)
                return self._receipt(db,'admin:'+actor,request_id,{'action':action,**payload},lambda:self._adjust(db,cid,amount,reason,actor,request_id))
            if action=='settings':return {'settings':self._settings(db)}
            if action=='announce':
                return self._receipt(db,'admin:'+actor,request_id,{'action':action,**payload},lambda:self._announce(db,actor,payload))
            if action=='announcements':return {'announcements':[dict(row) for row in db.execute('SELECT * FROM customer_announcements ORDER BY id DESC')]}
            if action=='save_settings':return self._save_settings(db,actor,payload)
            if action=='discounts':return {'discounts':[dict(row) for row in db.execute('SELECT * FROM discounts ORDER BY id DESC')]}
            if action=='save_discount':return self._save_discount(db,actor,payload)
            if action=='tickets':
                receipts=self._support_receipts(db)
                counts=Counter(receipt['customer_id'] for receipt in receipts)
                tickets=[dict(row,receipt_count=counts[row['customer_id']])
                         for row in db.execute('SELECT * FROM support_tickets ORDER BY id DESC')]
                return {'tickets':tickets,'receipts':receipts}
            if action in {'ticket_messages','reply_ticket'}:
                tid=integer(payload.get('id'),1);ticket=db.execute('SELECT * FROM support_tickets WHERE id=?',(tid,)).fetchone()
                if not ticket:fail('not_found','تیکت پیدا نشد.',404)
                if action=='reply_ticket':
                    return self._receipt(db,'admin:'+actor,request_id,{'action':action,**payload},lambda:self._reply_result(db,tid,'admin:'+actor,payload))
                return {'ticket':dict(ticket),
                        'messages':[dict(row) for row in db.execute('SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id',(tid,))],
                        'receipts':self._support_receipts(db,ticket['customer_id'])}
            if action=='ticket_attachment':
                row=db.execute('SELECT attachment_reference FROM support_messages WHERE ticket_id=? AND id=?',(integer(payload.get('id'),1),integer(payload.get('attachment_id'),1))).fetchone()
                if not row or not row['attachment_reference']:fail('not_found','پیوست پیدا نشد.',404)
                return {'attachment_reference':row['attachment_reference']}
            if action=='jobs':return {'jobs':[dict(row) for row in db.execute('SELECT * FROM provisioning_jobs ORDER BY id DESC')],'notifications':[dict(row) for row in db.execute('SELECT * FROM notification_jobs ORDER BY id DESC')]}
            if action=='audit':return {'audit':[dict(row) for row in db.execute('SELECT * FROM commerce_audit ORDER BY id DESC LIMIT 1000')]}
            if action=='health':return {'ok':True,'database':True,'native_configured':self.native is not None,'pending_jobs':db.execute("SELECT count(*) FROM provisioning_jobs WHERE status='pending'").fetchone()[0]}
            if action=='reconcile':
                jid=integer(payload.get('id'),1)
            else:fail('unknown_action','این عملیات پشتیبانی نمی‌شود.',404)
        return self.reconcile_job(jid,actor)

    def _adjust(self,db,cid,amount,reason,actor,key):
        self._ledger(db,cid,amount,'adjustment','adjust:'+actor+':'+key,reason)
        self._audit(db,actor,'wallet:adjust',cid)
        return {'balance_toman':self._balance(db,cid)}

    def _announce(self,db,actor,payload):
        if set(payload)!={'title','body'}:fail()
        title=text(payload['title'],160);body=text(payload['body'],2000)
        nid=db.execute('INSERT INTO customer_announcements(title,body,actor,created_at) VALUES(?,?,?,?)',(title,body,actor,self.now())).lastrowid
        customers=list(db.execute('SELECT id FROM customers WHERE suspended=0'))
        for customer in customers:self._notify(db,customer['id'],'announcement',{'title':title,'body':body},'customer-notice:'+str(nid)+':'+str(customer['id']))
        self._audit(db,actor,'announcement:create',nid)
        return {'announcement':dict(db.execute('SELECT * FROM customer_announcements WHERE id=?',(nid,)).fetchone()),'queued':len(customers)}

    def _save_product(self,db,actor,p):
        allowed={'id','name','category','traffic_gb','days','devices','price_toman','active','target','pricing'}
        if set(p)-allowed:fail()
        previous=None
        if p.get('id'):
            previous=db.execute('SELECT * FROM products WHERE id=?',(integer(p['id'],1),)).fetchone()
            if not previous:fail('not_found','محصول پیدا نشد.',404)
            p={**self._product(previous),**p};p.pop('unlimited',None);p.pop('created_at',None)
        name=text(p.get('name'),160);category=p.get('category','economy')
        if category not in {'economy','custom','unlimited'}:fail()
        traffic=p.get('traffic_gb');traffic=integer(traffic,1,1000000) if traffic is not None else None
        if category=='unlimited' and traffic is not None:fail('invalid_unlimited','محصول نامحدود نباید سقف ترافیک داشته باشد.')
        days=integer(p.get('days'),1,3650);devices=integer(p.get('devices',1),1,5);price=integer(p.get('price_toman'),1)
        active=p.get('active',False)
        if type(active) is not bool:fail()
        target=target_spec(p.get('target',{}),allow_empty=True)
        if active and not target and not self._settings(db)['bot_target']:
            fail('target_required','برای فروش فعال، اینترفیس فروش بات یا مقصد این طرح را تعیین کنید.')
        pricing=p.get('pricing',{})
        if not isinstance(pricing,dict) or len(dumps(pricing))>5000:fail()
        if set(pricing)-{'traffic_options','traffic_prices','duration_options','duration_percent','device_options','additional_device_toman'}:fail()
        for key,low,high in [('traffic_options',1,1000000),('duration_options',1,3650),('device_options',1,5)]:
            if key in pricing:
                if not isinstance(pricing[key],list) or not 1<=len(pricing[key])<=30:fail()
                for option in pricing[key]:integer(option,low,high)
        for key in ('traffic_prices','duration_percent'):
            if key in pricing:
                if not isinstance(pricing[key],dict) or len(pricing[key])>30:fail()
                for number,value in pricing[key].items():
                    if not isinstance(number,str) or not number.isdigit():fail()
                    integer(value,1,10000 if key=='duration_percent' else 10**12)
        if 'additional_device_toman' in pricing:integer(pricing['additional_device_toman'],0,10**12)
        values=(name,category,traffic,days,devices,price,int(active),dumps(target),dumps(pricing))
        if previous:
            pid=previous['id'];db.execute('UPDATE products SET name=?,category=?,traffic_gb=?,days=?,devices=?,price_toman=?,active=?,target=?,pricing=? WHERE id=?',values+(pid,))
        else:pid=db.execute('INSERT INTO products(name,category,traffic_gb,days,devices,price_toman,active,target,pricing,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',values+(self.now(),)).lastrowid
        self._audit(db,actor,'product:save',pid)
        return {'product':self._product(db.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())}

    def _save_destination(self,db,actor,p):
        if set(p)-{'id','card_number','cardholder_name','holder_name','bank_name','instructions','active'}:fail()
        previous=db.execute('SELECT * FROM payment_destinations WHERE id=?',(integer(p['id'],1),)).fetchone() if p.get('id') else None
        if p.get('id') and not previous:fail('not_found','مقصد پرداخت پیدا نشد.',404)
        if previous:p={**self._destination(previous),**p}
        card=p.get('card_number')
        if not isinstance(card,str) or not re.fullmatch(r'[0-9]{16}',card) or len(set(card))<2:fail('invalid_card','شماره کارت باید ۱۶ رقم معتبر داشته باشد.')
        holder=text(p.get('cardholder_name',p.get('holder_name')),160);bank=text(p.get('bank_name',''),80,False)
        instructions=text(p.get('instructions',self.DEFAULTS['payment_instructions']),2000,False)
        active=p.get('active',False)
        if type(active) is not bool:fail()
        if previous:
            did=previous['id'];db.execute('UPDATE payment_destinations SET card_number=?,holder_name=?,bank_name=?,instructions=?,active=?,updated_at=? WHERE id=?',(card,holder,bank,instructions,int(active),self.now(),did))
        else:did=db.execute('INSERT INTO payment_destinations(card_number,holder_name,bank_name,instructions,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(card,holder,bank,instructions,int(active),self.now(),self.now())).lastrowid
        destination=self._destination(db.execute('SELECT * FROM payment_destinations WHERE id=?',(did,)).fetchone())
        db.execute('INSERT INTO destination_audit(destination_id,actor,snapshot,created_at) VALUES(?,?,?,?)',(did,actor,dumps(destination),self.now()))
        self._audit(db,actor,'destination:save',did)
        return {'destination':destination}

    def _review_payment(self,db,actor,p):
        pid=integer(p.get('id'),1);decision=p.get('decision');reason=text(p.get('reason',''),1000,False)
        if decision not in {'approved','rejected','more_info'}:fail()
        if decision in {'rejected','more_info'} and not reason:fail('reason_required','دلیل رد یا درخواست اطلاعات را بنویسید.')
        row=db.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        if not row:fail('not_found','پرداخت پیدا نشد.',404)
        if row['status']=='approved' and decision=='approved':return {'payment':self._payment_value(row)}
        if row['status']!='pending_review':fail('payment_not_reviewable','این پرداخت در انتظار بررسی نیست.',409)
        status='pending_payment' if decision=='more_info' else decision
        db.execute('UPDATE payments SET status=?,reviewer=?,reviewed_at=?,reason=? WHERE id=?',(status,actor,self.now(),reason,pid))
        if decision=='approved':
            if row['kind']=='topup':self._ledger(db,row['customer_id'],row['amount_toman'],'topup','payment:'+str(pid),'شارژ کیف پول با تأیید مدیر')
            else:
                order=db.execute('SELECT * FROM orders WHERE id=?',(row['order_id'],)).fetchone()
                if order['status']!='pending_payment':fail('order_not_pending','سفارش قابل پرداخت نیست.',409)
                db.execute("UPDATE orders SET status='paid',updated_at=? WHERE id=?",(self.now(),order['id']))
                self._fulfillment_job(db,order['id'],row['customer_id'],json.loads(order['snapshot']))
        elif decision=='rejected' and row['order_id']:
            db.execute("UPDATE orders SET status='rejected',updated_at=? WHERE id=?",(self.now(),row['order_id']))
            db.execute('DELETE FROM discount_redemptions WHERE order_id=?',(row['order_id'],))
        self._audit(db,actor,'payment:'+decision,pid)
        self._notify(db,row['customer_id'],'payment_'+decision,{'payment_id':pid,'reason':reason},'payment-review:'+str(pid)+':'+decision)
        return {'payment':self._payment_value(db.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone())}

    def _refund_external(self,actor,p,key):
        oid=integer(p.get('id'),1);text(p.get('reason'),1000)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{16,100}',key):fail('idempotency_required','شناسهٔ یکتای درخواست الزامی است.')
        with self.store.db(write=True) as db:
            previous=db.execute('SELECT * FROM commerce_receipts WHERE actor=? AND request_id=?',('admin:'+actor,key)).fetchone()
            fingerprint=hashlib.sha256(dumps({'action':'refund_order',**p}).encode()).hexdigest()
            if previous:
                if previous['fingerprint']!=fingerprint:fail('idempotency_conflict','این درخواست قبلاً با اطلاعات متفاوت ثبت شده است.',409)
                return json.loads(previous['response'])
            order=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not order:fail('not_found','سفارش پیدا نشد.',404)
            job=db.execute('SELECT * FROM provisioning_jobs WHERE order_id=?',(oid,)).fetchone()
            sub=db.execute('SELECT * FROM subscriptions WHERE id=?',(job['subscription_id'],)).fetchone() if job else None
            if order['status']=='refunded':return {'order':self._order_value(db,order),'native_action_required':False}
            if order['status'] not in {'paid','active'}:fail('not_refundable','این سفارش قابل بازپرداخت نیست.',409)
            if job and job['status'] not in {'pending','cancelled','active'}:fail('provisioning_unresolved','ابتدا نتیجهٔ ساخت را بررسی کنید.',409)
            reward=db.execute('SELECT * FROM referral_rewards WHERE order_id=?',(oid,)).fetchone()
            if reward and self._balance(db,reward['referrer_id'])<reward['amount_toman']:fail('referral_credit_spent','پاداش معرفی قبلاً خرج شده است؛ ابتدا تسویهٔ معرفی را انجام دهید.',409)
            if sub and db.execute("SELECT 1 FROM provisioning_jobs WHERE subscription_id=? AND id!=? AND status IN ('pending','provisioning','uncertain')",(sub['id'],job['id'])).fetchone():
                fail('subscription_operation_pending','برای این اشتراک عملیات دیگری در انتظار است؛ ابتدا نتیجهٔ آن را بررسی کنید.',409)
            if sub:db.execute("UPDATE subscriptions SET status='refund_pending',updated_at=? WHERE id=?",(self.now(),sub['id']))
            if job and job['status']=='pending':
                # Durable exclusion prevents a worker from creating a service
                # between this check and the refund transaction.
                db.execute("UPDATE provisioning_jobs SET status='cancelled',updated_at=? WHERE id=?",(self.now(),job['id']))
        if sub and sub['native_id'] is not None:
            if self.native is None or not callable(getattr(self.native,'suspend',None)):fail('suspension_required','سرویس باید پیش از بازپرداخت متوقف شود.',409)
            try:
                result=self.native.suspend('commerce-refund-'+str(oid),{'existing_native_id':sub['native_id']})
                if not isinstance(result,dict) or result.get('status') not in {'suspended','expired'}:raise ValueError('suspension not confirmed')
            except Exception:fail('suspension_unconfirmed','توقف سرویس تأیید نشد؛ بازپرداخت مالی انجام نشده است.',503)
        with self.store.db(write=True) as db:
            return self._receipt(db,'admin:'+actor,key,{'action':'refund_order',**p},lambda:self._refund_order(db,actor,p))

    def _refund_order(self,db,actor,p):
        oid=integer(p.get('id'),1);reason=text(p.get('reason'),1000)
        row=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not row:fail('not_found','سفارش پیدا نشد.',404)
        if row['status']=='refunded':return {'order':self._order_value(db,row)}
        if row['status'] not in {'paid','active'}:fail('not_refundable','این سفارش قابل بازپرداخت نیست.',409)
        job=db.execute('SELECT * FROM provisioning_jobs WHERE order_id=?',(oid,)).fetchone()
        if job and job['status'] not in {'pending','cancelled','active'}:fail('provisioning_unresolved','ابتدا نتیجهٔ ساخت را بررسی کنید.',409)
        reward=db.execute('SELECT * FROM referral_rewards WHERE order_id=?',(oid,)).fetchone()
        if reward:
            self._ledger(db,reward['referrer_id'],-reward['amount_toman'],'referral_reversal','refund-referral:'+str(oid),'برگشت پاداش سفارش بازپرداخت‌شده')
        self._ledger(db,row['customer_id'],row['total_toman'],'refund','refund-order:'+str(oid),reason)
        db.execute("UPDATE orders SET status='refunded',updated_at=? WHERE id=?",(self.now(),oid))
        if job and job['status']=='pending':db.execute("UPDATE provisioning_jobs SET status='cancelled',updated_at=? WHERE order_id=?",(self.now(),oid))
        if job:db.execute("UPDATE subscriptions SET status='suspended',updated_at=? WHERE id=?",(self.now(),job['subscription_id']))
        self._audit(db,actor,'order:refund',oid)
        return {'order':self._order_value(db,db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()),'native_action_required':False}

    def _save_settings(self,db,actor,p):
        if set(p)-set(self.DEFAULTS):fail('invalid_settings','این تنظیم قابل تغییر نیست.')
        merged={**self._settings(db),**p}
        if merged['currency']!='toman' or merged['rial_per_toman']!=10:fail()
        if type(merged['trial_enabled']) is not bool:fail()
        for key,minimum,maximum in [('trial_hours',1,168),('trial_traffic_gb',1,100),('trial_capacity',1,100000),('referral_percent',0,100),('topup_min_toman',1,10**10),('topup_max_toman',1,10**10),('extra_device_price_toman',0,10**10),('expiry_notice_hours',1,720),('traffic_warning_percent',1,100)]:integer(merged[key],minimum,maximum)
        if merged['topup_max_toman']<merged['topup_min_toman']:fail()
        target_spec(merged['trial_target'],allow_empty=True)
        target_spec(merged['bot_target'],allow_empty=True)
        if merged['trial_enabled'] and not merged['trial_target']:
            fail('trial_unconfigured','پیش از فعال‌کردن تست رایگان، اینترفیس تست را انتخاب کنید.',409)
        if not isinstance(merged['topup_amounts'],list) or not 1<=len(merged['topup_amounts'])<=20:fail()
        for value in merged['topup_amounts']:integer(value,merged['topup_min_toman'],merged['topup_max_toman'])
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',merged['support_username']):fail()
        text(merged['payment_instructions'],2000,False)
        for key,value in p.items():db.execute('INSERT INTO commerce_settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',(key,dumps(value)))
        self._audit(db,actor,'settings:save',','.join(sorted(p)))
        return {'settings':merged}

    def _save_discount(self,db,actor,p):
        if set(p)-{'id','code','kind','value','active','expires_at','max_uses','per_customer','product_ids','first_purchase'}:fail()
        old=db.execute('SELECT * FROM discounts WHERE id=?',(integer(p['id'],1),)).fetchone() if p.get('id') else None
        if p.get('id') and not old:fail('not_found','کد تخفیف پیدا نشد.',404)
        if old:p={**dict(old),'active':bool(old['active']),'first_purchase':bool(old['first_purchase']),**p,'product_ids':p.get('product_ids',json.loads(old['product_ids']))}
        code=text(p.get('code'),64).upper()
        if not re.fullmatch(r'[A-Z0-9_-]{3,64}',code):fail()
        kind=p.get('kind');value=integer(p.get('value'),1,100 if kind=='percent' else 10**12)
        if kind not in {'percent','fixed'}:fail()
        active=p.get('active',False);first=p.get('first_purchase',False)
        if type(active) is not bool or type(first) not in {bool,int}:fail()
        products=p.get('product_ids',[])
        if not isinstance(products,list) or len(products)>100:fail()
        for pid in products:integer(pid,1)
        expires=p.get('expires_at');expires=integer(expires,1) if expires is not None else None
        maximum=p.get('max_uses');maximum=integer(maximum,1,1000000) if maximum is not None else None
        per=integer(p.get('per_customer',1),1,1000)
        values=(code,kind,value,int(active),expires,maximum,per,dumps(products),int(first))
        try:
            if old:
                did=old['id'];db.execute('UPDATE discounts SET code=?,kind=?,value=?,active=?,expires_at=?,max_uses=?,per_customer=?,product_ids=?,first_purchase=? WHERE id=?',values+(did,))
            else:did=db.execute('INSERT INTO discounts(code,kind,value,active,expires_at,max_uses,per_customer,product_ids,first_purchase) VALUES(?,?,?,?,?,?,?,?,?)',values).lastrowid
        except sqlite3.IntegrityError:fail('discount_exists','این کد تخفیف قبلاً ثبت شده است.',409)
        self._audit(db,actor,'discount:save',did)
        return {'discount':dict(db.execute('SELECT * FROM discounts WHERE id=?',(did,)).fetchone())}

    def process_jobs(self, limit=10):
        """Claim each durable intent once. Any ambiguous outcome needs reconciliation."""
        if self.native is None:return {'processed':0,'available':False}
        results=[]
        for _ in range(integer(limit,1,100)):
            with self.store.db(write=True) as db:
                row=db.execute("SELECT * FROM provisioning_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
                if not row:break
                db.execute("UPDATE provisioning_jobs SET status='provisioning',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'",(self.now(),row['id']))
                db.execute("UPDATE subscriptions SET status='provisioning',updated_at=? WHERE id=?",(self.now(),row['subscription_id']))
            try:
                spec=json.loads(row['spec'])
                if spec.get('action') in {'renew','upgrade'}:
                    method=getattr(self.native,spec['action'])
                    result=method(row['operation_id'],spec)
                else:result=self.native.provision(row['operation_id'],spec)
                if not isinstance(result,dict) or type(result.get('id')) is not int:raise ValueError('native result incomplete')
                self._job_success(row,result)
                results.append({'id':row['id'],'status':'active'})
            except Exception:
                with self.store.db(write=True) as db:
                    db.execute("UPDATE provisioning_jobs SET status='uncertain',error='native_outcome_requires_reconciliation',updated_at=? WHERE id=?",(self.now(),row['id']))
                    db.execute("UPDATE subscriptions SET status='failed',updated_at=? WHERE id=?",(self.now(),row['subscription_id']))
                    self._audit(db,'system','provision:uncertain',row['id'])
                results.append({'id':row['id'],'status':'uncertain'})
        return {'processed':len(results),'jobs':results}

    def _job_success(self,row,result):
        with self.store.db(write=True) as db:
            current=db.execute('SELECT * FROM provisioning_jobs WHERE id=?',(row['id'],)).fetchone()
            if current['status']=='active':return
            spec=json.loads(current['spec'])
            # Never persist configuration/private credentials in commerce snapshots.
            safe={k:result.get(k) for k in ('id','status','used_bytes','remaining_bytes','expires_at','expires_at_ts','remaining_seconds','limit_bytes')}
            self._native_time_fields(safe)
            safe['available']=True
            safe['peers']=[{k:p[k] for k in ('id','name','public_key') if k in p} for p in result.get('peers',[]) if isinstance(p,dict)]
            db.execute("UPDATE provisioning_jobs SET status='active',error=NULL,updated_at=? WHERE id=?",(self.now(),row['id']))
            db.execute("UPDATE subscriptions SET native_id=?,status='active',snapshot=?,native_snapshot=?,updated_at=? WHERE id=?",(result['id'],dumps(spec),dumps(safe),self.now(),row['subscription_id']))
            if row['order_id']:
                db.execute("UPDATE orders SET status='active',updated_at=? WHERE id=?",(self.now(),row['order_id']))
                self._referral_reward(db,row['order_id'])
            sub=db.execute('SELECT customer_id,trial FROM subscriptions WHERE id=?',(row['subscription_id'],)).fetchone()
            self._notify(db,sub['customer_id'],'trial_activation' if sub['trial'] else 'subscription_activation',{'subscription_id':row['subscription_id']},'subscription-active:'+row['operation_id'])
            self._audit(db,'system','provision:active',row['id'])

    def _referral_reward(self,db,oid):
        order=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        customer=db.execute('SELECT * FROM customers WHERE id=?',(order['customer_id'],)).fetchone()
        referrer=customer['referrer_id']
        if not referrer or referrer==customer['id'] or db.execute('SELECT 1 FROM referral_rewards WHERE referred_id=?',(customer['id'],)).fetchone():return
        first=db.execute("SELECT id FROM orders WHERE customer_id=? AND status='active' ORDER BY id LIMIT 1",(customer['id'],)).fetchone()
        if not first or first['id']!=oid:return
        amount=order['total_toman']*self._settings(db)['referral_percent']//100
        if amount<=0:return
        db.execute('INSERT INTO referral_rewards VALUES(?,?,?,?,?)',(customer['id'],referrer,oid,amount,self.now()))
        self._ledger(db,referrer,amount,'referral','referral:'+str(customer['id']),'پاداش اولین خرید دعوت‌شده')
        self._notify(db,referrer,'referral_reward',{'amount_toman':amount},'referral:'+str(customer['id']))

    def reconcile_job(self,jid,actor='system'):
        if self.native is None:fail('native_unavailable','ارتباط سرویس بومی تنظیم نشده است.',503)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM provisioning_jobs WHERE id=?',(jid,)).fetchone()
            if not row:fail('not_found','کار پیدا نشد.',404)
            if row['status']=='active':return {'job':dict(row)}
        try:result=self.native.reconcile(row['operation_id'])
        except Exception:fail('reconcile_unavailable','نتیجهٔ ساخت هنوز قابل تأیید نیست.',503)
        if isinstance(result,dict) and type(result.get('id')) is int:self._job_success(row,result)
        else:
            # Absence is not proof that remote side effects never occurred.
            fail('reconciliation_required','ساخت قبلی تأیید نشده است؛ تکرار خودکار انجام نمی‌شود.',409)
        with self.store.db() as db:return {'job':dict(db.execute('SELECT * FROM provisioning_jobs WHERE id=?',(jid,)).fetchone())}

    def refresh_subscriptions(self, limit=100):
        if self.native is None:return {'refreshed':0}
        with self.store.db() as db:rows=list(db.execute('SELECT * FROM subscriptions WHERE native_id IS NOT NULL ORDER BY updated_at,id LIMIT ?',(integer(limit,1,1000),)))
        count=0
        for row in rows:
            try:value=self.native.subscription(row['native_id'])
            except Exception:value=None
            if not isinstance(value,dict):
                cached=json.loads(row['native_snapshot'] or '{}');cached['available']=False
                with self.store.db(write=True) as db:db.execute('UPDATE subscriptions SET native_snapshot=?,updated_at=? WHERE id=?',(dumps(cached),self.now(),row['id']))
                continue
            safe={k:value.get(k) for k in ('id','status','used_bytes','remaining_bytes','expires_at','expires_at_ts','remaining_seconds','limit_bytes')}
            self._native_time_fields(safe)
            safe['available']=True
            safe['peers']=[{k:p[k] for k in ('id','name','public_key') if k in p} for p in value.get('peers',[]) if isinstance(p,dict)]
            status=value.get('status','active')
            if status not in {'active','suspended','expired','failed'}:status='active'
            with self.store.db(write=True) as db:
                db.execute("UPDATE subscriptions SET status=CASE WHEN status='refund_pending' THEN status ELSE ? END,native_snapshot=?,updated_at=? WHERE id=?",(status,dumps(safe),self.now(),row['id']))
                if status=='expired':self._notify(db,row['customer_id'],'subscription_expired',{'subscription_id':row['id']},'expired:'+str(row['id']))
                settings=self._settings(db);remaining=safe.get('remaining_bytes');traffic=json.loads(row['snapshot']).get('traffic_gb')
                if traffic and remaining is not None and remaining<=traffic*1024**3*settings['traffic_warning_percent']//100:self._notify(db,row['customer_id'],'traffic_near_limit',{'subscription_id':row['id']},'traffic-low:'+str(row['id']))
                expiry=safe.get('expires_at_ts')
                if isinstance(expiry,(int,float)) and 0<expiry-self.now()<=settings['expiry_notice_hours']*3600:self._notify(db,row['customer_id'],'subscription_near_expiry',{'subscription_id':row['id']},'expiry-near:'+str(row['id']))
            count+=1
        return {'refreshed':count}

    def expire_payments(self):
        with self.store.db(write=True) as db:
            rows=list(db.execute("SELECT * FROM payments WHERE status='pending_payment' AND expires_at<=?",(self.now(),)))
            for row in rows:
                db.execute("UPDATE payments SET status='expired' WHERE id=?",(row['id'],))
                if row['order_id']:db.execute("UPDATE orders SET status='expired',updated_at=? WHERE id=? AND status='pending_payment'",(self.now(),row['order_id']))
                if row['order_id']:db.execute('DELETE FROM discount_redemptions WHERE order_id=?',(row['order_id'],))
            return {'expired':len(rows)}

    def claim_notifications(self,limit=20,lease_seconds=60):
        with self.store.db(write=True) as db:
            rows=list(db.execute("SELECT n.*,c.telegram_id FROM notification_jobs n LEFT JOIN customers c ON c.id=n.customer_id WHERE n.status IN ('pending','retry','sending') AND n.available_at<=? AND n.lease_until<=? ORDER BY n.id LIMIT ?",(self.now(),self.now(),integer(limit,1,100))))
            for row in rows:db.execute("UPDATE notification_jobs SET status='sending',lease_until=?,attempts=attempts+1 WHERE id=?",(self.now()+integer(lease_seconds,10,600),row['id']))
            return [{**dict(row),'payload':json.loads(row['payload'])} for row in rows]

    def finish_notification(self,job_id,ok,retry_after=None):
        with self.store.db(write=True) as db:
            row=db.execute('SELECT * FROM notification_jobs WHERE id=?',(integer(job_id,1),)).fetchone()
            if not row:return
            if ok:db.execute("UPDATE notification_jobs SET status='sent',lease_until=0,last_error=NULL WHERE id=?",(job_id,))
            else:
                delay=max(1,min(86400,int(retry_after or 2**min(row['attempts'],12))))
                status='failed' if row['attempts']>=12 else 'retry'
                db.execute('UPDATE notification_jobs SET status=?,available_at=?,lease_until=0,last_error=? WHERE id=?',(status,self.now()+delay,'telegram_delivery_failed',job_id))
