"""Public customer bot. Polling is supervised by the existing panel process.

No inbound unauthenticated webhook is installed. Customer identity comes only
from private updates retrieved over the authenticated Telegram Bot API.
"""
import hashlib
import json
import re
import threading
import time
from urllib.parse import urlsplit


MENU=[('🛒 خرید اشتراک','shop'),('🧪 دریافت تست رایگان','trial'),('📦 اشتراک‌های من','subscriptions'),
      ('💰 کیف پول','wallet'),('🎁 دعوت دوستان','referrals'),('📚 آموزش اتصال','guides'),
      ('📥 دانلود برنامه‌ها','downloads'),('🎧 پشتیبانی','support'),('🚀 ورود به پنل کاربری','miniapp')]
CATEGORIES={'economy':'🔥 طرح‌های اقتصادی','custom':'📊 اشتراک حجمی','unlimited':'♾ اشتراک نامحدود'}
PRICE_MENU='📋 طرح‌ها و قیمت‌ها'
MENU_ACTIONS={**dict(MENU),PRICE_MENU:'prices'}
GUIDES='📚 راهنمای اتصال WireGuard\n۱. برنامهٔ رسمی WireGuard را نصب کنید.\n۲. فایل کانفیگ را وارد کنید یا کد QR اشتراک را اسکن کنید.\n۳. اتصال را روشن کنید.\nدر ویندوز و مک از Import tunnel from file و در اندروید و iOS از افزودن تونل استفاده کنید.'
DOWNLOADS='📥 برنامه‌های رسمی WireGuard برای اندروید، iOS، ویندوز و macOS:\nhttps://www.wireguard.com/install/'


def keyboard(rows):
    return {'inline_keyboard':[[{'text':label,'callback_data':value} for label,value in line] for line in rows]}


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_updates (bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(bot_id,update_id))')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_states (bot_id TEXT NOT NULL, telegram_id INTEGER NOT NULL, value TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(bot_id,telegram_id))')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_offsets (bot_id TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_outbox (id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, customer_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_notification_deliveries (bot_id TEXT NOT NULL, job_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, part INTEGER NOT NULL, PRIMARY KEY(bot_id,job_id,chat_id,part))')


class PublicBot:
    def __init__(self,service,transport,base_url,token_provider,*,artifact_provider=None,administrator_ids_provider=None,admin_provider=None,clock=None,bot_username=None):
        parsed=urlsplit(base_url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('public_miniapp_https_required')
        self.service=service;self.store=service.store;self.transport=transport;self.base_url=base_url.rstrip('/')
        self.token_provider=token_provider;self.artifact_provider=artifact_provider;self.clock=clock or time.time
        self.administrator_ids_provider=administrator_ids_provider or admin_provider or (lambda:[])
        self.bot_username=bot_username if isinstance(bot_username,str) and re.fullmatch(r'[A-Za-z0-9_]{5,64}',bot_username) else None
        self.lock=threading.RLock()

    def _token(self):
        token=self.token_provider()
        if not isinstance(token,str) or not re.fullmatch(r'[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,120}',token):raise ValueError('public_bot_not_configured')
        return token

    def _bot(self):return self._token().split(':')[0]

    def main(self):
        rows=[[MENU[0][0]],[MENU[1][0]],[MENU[2][0],MENU[3][0]],
              [PRICE_MENU,MENU[5][0]],[MENU[4][0],MENU[7][0]],[MENU[6][0]]]
        buttons=[[{'text':label} for label in row] for row in rows]
        buttons.append([{'text':MENU[-1][0],'web_app':{'url':self.base_url+'/telegram-app'}}])
        markup={'keyboard':buttons,'resize_keyboard':True,'is_persistent':True}
        return {'text':'به WG Portal خوش آمدید. گزینهٔ موردنظر را انتخاب کنید.','reply_markup':markup}

    def _action(self,cid,action,payload=None,key=None):
        return self.service.customer_action(cid,action,payload or {},request_id=key)

    def _message(self,text,rows=None):
        return {'text':text,'reply_markup':keyboard((rows or [])+[[('🏠 منوی اصلی','home')]])}

    def _state(self,bot,uid):
        with self.store.db(write=True) as db:
            _ensure(db)
            row=db.execute('SELECT * FROM commerce_public_states WHERE bot_id=? AND telegram_id=?',(bot,uid)).fetchone()
        return json.loads(row['value']) if row and row['expires_at']>int(self.clock()) else None

    @staticmethod
    def _private(update):
        if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0:return None
        callback=update.get('callback_query') or {}
        message=callback.get('message') or update.get('message') or {}
        sender=callback.get('from') or message.get('from') or {}
        chat=message.get('chat') or {}
        uid=sender.get('id')
        if type(uid) is not int or not 0<uid<2**52 or sender.get('is_bot') or chat.get('type')!='private' or chat.get('id')!=uid:return None
        return sender,message,callback

    def handle_update(self,update):
        """Internal consumer entry point, never registered as a public route."""
        identity=self._private(update)
        if identity is None:return False
        sender,message,callback=identity
        bot=self._bot();uid=sender['id'];update_id=update['update_id']
        with self.lock:
            with self.store.db(write=True) as db:
                _ensure(db)
                if db.execute('SELECT 1 FROM commerce_public_updates WHERE bot_id=? AND update_id=? AND done=1',(bot,update_id)).fetchone():return True
            text=str(message.get('text') or '').strip()
            referral=text[11:] if text.startswith('/start ref_') else None
            customer=self.service.customer_from_telegram({key:sender.get(key,'') for key in ('id','first_name','last_name','username')},referral_code=referral)
            cid=customer['id'];key='public-bot-'+bot+'-'+str(update_id)
            state=self._state(bot,uid)
            try:
                messages,new_state=self._route(cid,text,callback.get('data'),state,key)
            except ValueError as error:
                # Domain error codes are mapped locally, never reflect internal
                # exception text, tokens, native responses or private keys.
                code=str(getattr(error,'code',''))
                friendly={'trial_already_used':'تست رایگان قبلاً دریافت شده است.',
                    'insufficient_balance':'موجودی کیف پول کافی نیست.',
                    'payment_unconfigured':'پرداخت هنوز توسط مدیر تنظیم نشده است.',
                    'customer_suspended':'دسترسی حساب متوقف شده است؛ با پشتیبانی تماس بگیرید.'}.get(code,'عملیات انجام نشد؛ وضعیت درخواست را در پنل بررسی کنید و کمی بعد تلاش کنید.')
                messages,new_state=[self._message(friendly)],None
            with self.store.db(write=True) as db:
                _ensure(db)
                if db.execute('SELECT 1 FROM commerce_public_updates WHERE bot_id=? AND update_id=? AND done=1',(bot,update_id)).fetchone():return True
                if new_state is None:db.execute('DELETE FROM commerce_public_states WHERE bot_id=? AND telegram_id=?',(bot,uid))
                else:db.execute('INSERT INTO commerce_public_states VALUES(?,?,?,?) ON CONFLICT(bot_id,telegram_id) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at',(bot,uid,json.dumps(new_state,ensure_ascii=False),int(self.clock())+600))
                for item in messages:
                    chunks=([dict(item,text=item['text'][start:start+1500]) for start in range(0,len(item['text']),1500)]
                            if isinstance(item.get('text'),str) and len(item['text'].encode('utf-16-le'))//2>4000 else [item])
                    for chunk in chunks:db.execute('INSERT INTO commerce_public_outbox(bot_id,customer_id,chat_id,payload) VALUES(?,?,?,?)',(bot,cid,uid,json.dumps(chunk,ensure_ascii=False)))
                db.execute('INSERT INTO commerce_public_updates VALUES(?,?,1) ON CONFLICT(bot_id,update_id) DO UPDATE SET done=1',(bot,update_id))
            return True

    def _route(self,cid,text,data,state,key):
        if data is None and text in MENU_ACTIONS:
            data=MENU_ACTIONS[text]
        if data is not None and (not isinstance(data,str) or len(data)>64):return [self._message('گزینه معتبر نیست.')],None
        if text.startswith('/start') or text in {'/cancel','/menu'} or data=='home':return [self.main()],None
        if state and state.get('product'):
            selection=state['selection']
            if data=='back:traffic':
                data='product:'+str(selection['product_id'])
            elif data=='back:duration':
                state['step']='traffic'
                data='traffic:'+str(selection['traffic_gb'])
            elif data=='back:devices':
                state['step']='duration'
                data='duration:'+str(selection['days'])
        if data=='miniapp':return [self.main()],None
        if data=='shop':
            products=self._action(cid,'products').get('products',[])
            available={p.get('category') for p in products}
            rows=[[(label,'categoryshop:'+kind)] for kind,label in CATEGORIES.items() if kind in available]
            if not rows and products:rows=[[('📋 همهٔ طرح‌ها','prices')]]
            rows.append([('🧪 دریافت تست رایگان','trial')])
            return [self._message('نوع اشتراک WireGuard را انتخاب کنید.' if products else 'در حال حاضر طرح فعالی وجود ندارد.',rows)],None
        category=re.fullmatch(r'categoryshop:(economy|custom|unlimited)',data or '')
        if data=='prices' or category:
            products=self._action(cid,'products').get('products',[])
            if category:products=[p for p in products if p.get('category')==category[1]]
            rows=[[(str(p.get('name','اشتراک'))[:50]+' · '+format(p.get('price_toman',0),',')+' تومان','product:'+str(p['id']))] for p in products[:80]]
            rows.append([('⬅️ دسته‌بندی طرح‌ها','shop')])
            return [self._message('🛒 طرح موردنظر را انتخاب کنید.' if products else 'در حال حاضر طرح فعالی وجود ندارد.',rows)],None
        match=re.fullmatch(r'product:([1-9][0-9]*)',data or '')
        if match:
            product=next((p for p in self._action(cid,'products').get('products',[]) if p['id']==int(match[1])),None)
            if product is None:return [self._message('طرح در دسترس نیست.')],None
            pricing=product.get('pricing') or {}
            selection={'product_id':product['id'],'traffic_gb':product.get('traffic_gb'),'days':product['days'],'devices':product['devices']}
            state={'step':'traffic','selection':selection,'product':product}
            options=list(dict.fromkeys([product.get('traffic_gb')]+pricing.get('traffic_options',[])))
            rows=[[(('نامحدود' if amount is None else str(amount)+' گیگابایت'),'traffic:'+str(amount))] for amount in options]
            category=product.get('category')
            rows.append([('⬅️ طرح‌ها','categoryshop:'+category if category in CATEGORIES else 'prices')])
            return [self._message('حجم اشتراک را انتخاب کنید.',rows)],state
        match=re.fullmatch(r'traffic:(None|[1-9][0-9]*)',data or '')
        if match and state and state.get('step')=='traffic':
            traffic=None if match[1]=='None' else int(match[1]);product=state['product'];pricing=product.get('pricing') or {}
            if traffic not in [product.get('traffic_gb')]+pricing.get('traffic_options',[]):raise ValueError('invalid_traffic')
            state['selection']['traffic_gb']=traffic;state['step']='duration'
            options=list(dict.fromkeys([product['days']]+pricing.get('duration_options',[])))
            rows=[[(str(days)+' روز','duration:'+str(days))] for days in options]
            rows.append([('⬅️ انتخاب حجم','back:traffic')])
            return [self._message('مدت اشتراک را انتخاب کنید.',rows)],state
        match=re.fullmatch(r'duration:([1-9][0-9]*)',data or '')
        if match and state and state.get('step')=='duration':
            days=int(match[1]);product=state['product'];pricing=product.get('pricing') or {}
            if days not in [product['days']]+pricing.get('duration_options',[]):raise ValueError('invalid_duration')
            state['selection']['days']=days;state['step']='devices'
            options=list(dict.fromkeys([product['devices']]+pricing.get('device_options',[])))
            rows=[[(str(count)+' دستگاه','devices:'+str(count))] for count in options]
            rows.append([('⬅️ انتخاب مدت','back:duration')])
            return [self._message('تعداد دستگاه را انتخاب کنید.',rows)],state
        match=re.fullmatch(r'devices:([1-5])',data or '')
        if match and state and state.get('step')=='devices':
            selection=dict(state['selection'],devices=int(match[1]))
            quote=self._action(cid,'quote',selection,key).get('quote',{})
            qid=str(quote['id'])
            rows=[[('💳 کارت‌به‌کارت','buy:card'),('💰 کیف پول','buy:wallet')],
                  [('🎟 کد تخفیف','discount')],[('⬅️ تعداد دستگاه','back:devices')]]
            traffic=quote.get('traffic_gb')
            volume='نامحدود' if traffic is None else f'{traffic} گیگابایت'
            return [self._message('🧾 پیش‌فاکتور\nطرح: '+str(quote.get('product_name',''))+'\nحجم: '+volume+'\nمدت: '+str(quote.get('days',''))+' روز\nدستگاه: '+str(quote.get('devices',''))+'\nقیمت پایه: '+format(quote.get('base_toman',0),',')+' تومان\nتخفیف: '+format(quote.get('discount_toman',0),',')+' تومان\nمبلغ نهایی: '+format(quote['total_toman'],',')+' تومان\nپرداخت را انتخاب کنید.',rows)],dict(state,step='invoice',quote_id=qid,selection=selection)
        if data=='discount' and state and state.get('step')=='invoice':return [self._message('کد تخفیف را ارسال کنید.')],dict(state,step='discount')
        if state and state.get('step')=='discount' and not data:
            quote=self._action(cid,'quote',dict(state['selection'],discount_code=text[:64]),key)['quote']
            return [self._message('مبلغ پس از تخفیف: '+format(quote['total_toman'],',')+' تومان',[[('💳 کارت‌به‌کارت','buy:card'),('💰 کیف پول','buy:wallet')]])],dict(state,step='invoice',quote_id=str(quote['id']))
        if data in {'buy:card','buy:wallet'} and state and state.get('step')=='invoice':
            result=self._action(cid,'orders',{'quote_id':state['quote_id'],'payment_method':'card' if data=='buy:card' else 'wallet'},key)
            order=result['order'];payment=result.get('payment') or {}
            if payment:
                return self._payment_messages(order,payment),None
            return [self._message('✅ سفارش '+str(order['id'])+' ثبت شد. وضعیت فعال‌سازی را در اشتراک‌های من ببینید.',[[('📦 اشتراک‌های من','subscriptions')]])],None
        if data=='trial':
            result=self._action(cid,'trial',{},key)
            subscription=result.get('subscription') or result.get('order') or {}
            return [self._message('🧪 درخواست تست ثبت شد. وضعیت: '+str(subscription.get('status','در حال آماده‌سازی')),[[('📦 اشتراک‌های من','subscriptions')]])],None
        if data=='wallet':
            wallet=self._action(cid,'wallet')
            lines=['💰 موجودی: '+format(wallet.get('balance_toman',0),',')+' تومان']
            for row in wallet.get('transactions',[])[:10]:lines.append(str(row.get('amount_toman',row.get('amount',0)))+' تومان · '+str(row.get('kind','تراکنش')))
            return [self._message('\n'.join(lines),[[('➕ شارژ کیف پول','topup')]])],None
        if data=='topup':return [self._message('مبلغ شارژ را به تومان، فقط عدد وارد کنید.')],{'step':'topup'}
        if state and state.get('step')=='topup' and not data:
            normalized=text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
            if not re.fullmatch(r'[0-9]{1,10}',normalized):return [self._message('مبلغ معتبر به تومان وارد کنید.')],state
            result=self._action(cid,'topup',{'amount_toman':int(normalized)},key)
            return self._payment_messages(result.get('order',{}),result['payment']),None
        match=re.fullmatch(r'receipt:([1-9][0-9]*)',data or '')
        if match:return [self._message('شماره پیگیری و توضیح تراکنش را ارسال کنید. برای بارگذاری تصویر رسید از پنل کاربری استفاده کنید.')],{'step':'receipt','payment_id':int(match[1])}
        if state and state.get('step')=='receipt' and not data:
            self._action(cid,'submit_receipt',{'payment_id':state['payment_id'],'transaction_text':text[:1000]},key)
            return [self._message('⏳ رسید برای بررسی مدیر ثبت شد. تا تأیید پرداخت، اشتراک فعال نمی‌شود.')],None
        if data=='subscriptions':
            subs=self._action(cid,'subscriptions').get('subscriptions',[])
            rows=[[(str(s.get('product_name',s.get('name','اشتراک')))[:35]+' · '+str(s['id']),'subscription:'+str(s['id']))] for s in subs[:80]]
            return [self._message('📦 اشتراک‌های شما' if rows else 'هنوز اشتراکی ندارید.',rows)],None
        match=re.fullmatch(r'subscription:([1-9][0-9]*)',data or '')
        if match:
            sid=int(match[1]);sub=self._action(cid,'subscription',{'id':sid})['subscription'];rows=[]
            for peer in sub.get('peers',[]):
                rows.append([('📄 فایل دستگاه '+str(peer['id']),'config:'+str(sid)+':'+str(peer['id'])),('▦ کد QR','qr:'+str(sid)+':'+str(peer['id']))])
            rows.append([('🔄 تمدید','renew:'+str(sid)),('⬆️ ارتقا','upgrade:'+str(sid))])
            return [self._message('اشتراک '+str(sid)+'\nوضعیت: '+str(sub.get('status',''))+'\nمصرف ثبت‌شده: '+str(sub.get('used_bytes','نامشخص'))+' بایت\nزمان باقی‌مانده: '+str(sub.get('remaining_seconds','نامشخص'))+' ثانیه',rows)],None
        match=re.fullmatch(r'(config|qr):([1-9][0-9]*):([1-9][0-9]*)',data or '')
        if match:
            sid,pid=int(match[2]),int(match[3])
            sub=self._action(cid,'subscription',{'id':sid})['subscription']
            if pid not in {p.get('id') for p in sub.get('peers',[])}:raise ValueError('peer_not_owned')
            return [{'artifact':{'subscription_id':sid,'peer_id':pid,'kind':match[1]}}],None
        match=re.fullmatch(r'(renew|upgrade):([1-9][0-9]*)',data or '')
        if match:
            products=self._action(cid,'products').get('products',[])
            return [self._message('طرح تمدید یا ارتقا را انتخاب کنید.',[[(str(product['name'])[:40],match[1]+'product:'+match[2]+':'+str(product['id']))] for product in products[:80]])],None
        match=re.fullmatch(r'(renew|upgrade)product:([1-9][0-9]*):([1-9][0-9]*)',data or '')
        if match:
            quote=self._action(cid,match[1]+'_quote',{'id':int(match[2]),'product_id':int(match[3])},key)['quote']
            return [self._message('پیش‌فاکتور تمدید/ارتقا: '+format(quote['total_toman'],',')+' تومان',[[('💳 کارت‌به‌کارت','buy:card'),('💰 کیف پول','buy:wallet')]])],{'step':'invoice','quote_id':str(quote['id'])}
        if data=='referrals':
            result=self._action(cid,'referrals')
            username=self.bot_username
            if not username:
                identity=self.transport.call(self._token(),'getMe',timeout=(2,3))
                if not isinstance(identity,dict) or str(identity.get('id'))!=self._bot() or not re.fullmatch(r'[A-Za-z0-9_]{5,64}',str(identity.get('username',''))):raise ValueError('bot_identity_unavailable')
                username=identity['username']
            link='https://t.me/'+username+'?start=ref_'+result['referral_code']
            return [self._message('🎁 دعوت دوستان\n'+link+'\nدعوت‌ها: '+str(result.get('referral_count',0))+'\nپاداش تأییدشده: '+format(result.get('earnings_toman',0),',')+' تومان')],None
        if data in {'guides','downloads'}:return [self._message(GUIDES if data=='guides' else DOWNLOADS)],None
        if data=='support':
            tickets=self._action(cid,'tickets').get('tickets',[])
            rows=[[(str(t.get('category','پشتیبانی'))+' · '+str(t['id']),'ticket:'+str(t['id']))] for t in tickets[:20]]
            rows.insert(0,[('✍️ تیکت جدید','ticketnew')])
            return [self._message('🎧 پشتیبانی؛ تیکت خود را انتخاب کنید.',rows)],None
        if data=='ticketnew':return [self._message('موضوع را انتخاب کنید.',[[('پرداخت','category:payment'),('اتصال','category:connection')],[('اشتراک','category:subscription'),('کیف پول','category:wallet')],[('سایر','category:other')]])],None
        if data and data.startswith('category:'):
            category=data.split(':')[1]
            if category not in {'payment','connection','subscription','wallet','other'}:raise ValueError('invalid_category')
            return [self._message('پیام خود را ارسال کنید. تصویر را می‌توانید از پنل کاربری پیوست کنید.')],{'step':'ticket','category':category}
        if state and state.get('step')=='ticket' and not data:
            result=self._action(cid,'create_ticket',{'category':state['category'],'body':text[:4000]},key)
            return [self._message('✅ تیکت ثبت شد. پاسخ مدیر در همین بخش قابل مشاهده است.')],None
        match=re.fullmatch(r'ticket:([1-9][0-9]*)',data or '')
        if match:
            tid=int(match[1]);rows=self._action(cid,'ticket_messages',{'id':tid}).get('messages',[])
            items=[self._message(str(row.get('body',''))[:3500]) for row in rows[-12:]]
            return items+[self._message('برای پاسخ، متن پیام خود را ارسال کنید.')],{'step':'ticket_reply','id':tid}
        if state and state.get('step')=='ticket_reply' and not data:
            self._action(cid,'reply_ticket',{'id':state['id'],'body':text[:4000]},key)
            return [self._message('✅ پاسخ شما ثبت شد.')],None
        return [self.main()],None

    def _payment_messages(self,order,payment):
        destination=payment.get('payment_destination') or order.get('payment_destination') or {}
        card=destination.get('card_number','');name=destination.get('cardholder_name',destination.get('holder_name',''))
        return [self._message('💳 پرداخت کارت‌به‌کارت\nسفارش: '+str(order.get('id',payment.get('id','')))+'\nمبلغ: '+format(payment.get('amount_toman',order.get('total_toman',0)),',')+' تومان\nشماره کارت: '+str(card)+'\nصاحب کارت: '+str(name)+'\nبانک: '+str(destination.get('bank_name',''))+'\n'+str(destination.get('instructions',''))+'\nپس از پرداخت شماره پیگیری یا تصویر رسید را ثبت کنید. تأیید پرداخت فقط توسط مدیر انجام می‌شود.',[[('📤 ثبت رسید','receipt:'+str(payment['id']))]])]

    def _send_artifact(self,token,chat_id,kind,body):
        payload={'chat_id':chat_id,'filename':'wireguard.conf' if kind=='config' else 'wireguard-qr.png'}
        try:self.transport.call(token,'sendDocument' if kind=='config' else 'sendPhoto',payload,**({'document':body} if kind=='config' else {'photo':body}))
        except Exception as error:
            # Retry as a document only after Telegram definitely rejected the
            # photo. Ambiguous network errors stay in the delivery queue.
            if kind!='qr' or getattr(error,'code','')!='telegram_bad_request':raise
            self.transport.call(token,'sendDocument',payload,document=body)

    def flush_outbox(self,limit=20):
        bot=self._bot();token=self._token();now=int(self.clock());sent=0
        with self.store.db(write=True) as db:
            _ensure(db)
            rows=db.execute('SELECT * FROM commerce_public_outbox WHERE bot_id=? AND sent=0 AND next_at<=? AND locked_until<=? ORDER BY id LIMIT ?',(bot,now,now,max(1,min(100,limit)))).fetchall()
        for row in rows:
            with self.store.db(write=True) as db:
                if db.execute('UPDATE commerce_public_outbox SET locked_until=? WHERE id=? AND sent=0 AND locked_until<=?',(now+60,row['id'],now)).rowcount!=1:continue
            try:
                payload=json.loads(row['payload'])
                if 'artifact' in payload:
                    artifact=payload['artifact']
                    if not callable(self.artifact_provider):raise ValueError('artifact_provider_missing')
                    body,_=self.artifact_provider(row['customer_id'],artifact['subscription_id'],artifact['peer_id'],artifact['kind'])
                    self._send_artifact(token,row['chat_id'],artifact['kind'],body)
                else:self.transport.call(token,'sendMessage',{'chat_id':row['chat_id'],'disable_web_page_preview':True,**payload})
            except Exception as error:
                attempts=row['attempts']+1;delay=max(min(3600,2**min(attempts,11)),int(getattr(error,'retry_after',0) or 0))
                with self.store.db(write=True) as db:
                    db.execute('UPDATE commerce_public_outbox SET attempts=?,next_at=?,locked_until=0,sent=? WHERE id=?',(attempts,now+delay,-1 if attempts>=12 else 0,row['id']))
                continue
            with self.store.db(write=True) as db:db.execute('UPDATE commerce_public_outbox SET sent=1,locked_until=0 WHERE id=?',(row['id'],))
            sent+=1
        return sent

    def notify_pending(self,limit=20):
        claim=getattr(self.service,'claim_notifications',None)
        if not callable(claim):return 0
        templates={'registration':'👋 حساب شما در WG Portal ثبت شد.',
            'trial_activation':'🧪 تست رایگان شما فعال شد.',
            'subscription_activation':'✅ اشتراک شما فعال شد.',
            'payment_submitted':'⏳ رسید پرداخت ثبت شد و در انتظار بررسی مدیر است.',
            'payment_approved':'✅ پرداخت شما توسط مدیر تأیید شد.',
            'payment_rejected':'❌ پرداخت شما تأیید نشد؛ دلیل را در پنل کاربری ببینید.',
            'payment_more_info':'📝 پرداخت شما به اطلاعات بیشتری نیاز دارد.',
            'wallet_credit':'💰 کیف پول شما شارژ شد.',
            'wallet_credited':'💰 کیف پول شما شارژ شد.',
            'wallet_refund':'↩️ مبلغ بازپرداخت به کیف پول شما اضافه شد.',
            'subscription_near_expiry':'⏳ اعتبار اشتراک شما رو به پایان است.',
            'subscription_expired':'⌛ اعتبار اشتراک شما تمام شده است.',
            'traffic_near_limit':'📉 حجم اشتراک شما رو به پایان است.',
            'referral_reward':'🎁 پاداش دعوت به کیف پول شما اضافه شد.',
            'support_reply':'🎧 پاسخ پشتیبانی ثبت شد؛ آن را در پنل بخوانید.',
            'payment_review':'💳 یک رسید پرداخت نیازمند بررسی مدیر است.',
            'admin_payment_submitted':'💳 یک رسید پرداخت نیازمند بررسی مدیر است.',
            'admin_registration':'👤 مشتری تلگرامی جدید ثبت شد.',
            'announcement':'📣 اطلاعیه WG Portal'}
        delivered=0
        for job in claim(limit=limit):
            text=templates.get(job.get('kind'))
            if text is None:
                self.service.finish_notification(job['id'],False,retry_after=3600);continue
            value=job.get('payload') or {}
            if job.get('kind')=='announcement':text+='\n'+str(value.get('title',''))+'\n\n'+str(value.get('body',''))
            elif job.get('kind') in {'payment_rejected','payment_more_info'}:text+='\n'+str(value.get('reason',''))[:1000]
            elif job.get('kind')=='support_reply':text+='\n'+str(value.get('body',''))[:4000]
            elif job.get('kind')=='referral_reward' and type(value.get('amount_toman')) is int:text+='\n'+format(value['amount_toman'],',')+' تومان'
            elif job.get('kind') in {'admin_payment_submitted','admin_registration'}:
                if type(value.get('customer_id')) is int:text+='\nشناسهٔ مشتری: '+str(value['customer_id'])
                if type(value.get('payment_id')) is int:text+='\nشناسهٔ پرداخت: '+str(value['payment_id'])
                if type(value.get('amount_toman')) is int:text+='\nمبلغ: '+format(value['amount_toman'],',')+' تومان'
            targets=[job['telegram_id']] if type(job.get('telegram_id')) is int and job['telegram_id']>0 else []
            if job.get('customer_id') is None:
                targets=[uid for uid in self.administrator_ids_provider() if type(uid) is int and 0<uid<2**52]
            try:
                if not targets:raise ValueError('notification_destination_unavailable')
                for target in targets:
                    for part,start in enumerate(range(0,len(text),1500)):
                        with self.store.db(write=True) as db:
                            _ensure(db)
                            already=db.execute('SELECT 1 FROM commerce_public_notification_deliveries WHERE bot_id=? AND job_id=? AND chat_id=? AND part=?',(self._bot(),job['id'],target,part)).fetchone()
                        if already:continue
                        button=({'text':'⚙️ بررسی در پنل مدیر','url':self.base_url+'/telegram-commerce'} if job.get('customer_id') is None
                                else {'text':'🚀 پنل کاربری','web_app':{'url':self.base_url+'/telegram-app'}})
                        self.transport.call(self._token(),'sendMessage',{'chat_id':target,'text':text[start:start+1500],
                            'reply_markup':{'inline_keyboard':[[button]]}})
                        with self.store.db(write=True) as db:
                            db.execute('INSERT OR IGNORE INTO commerce_public_notification_deliveries VALUES(?,?,?,?)',(self._bot(),job['id'],target,part))
                    if job.get('kind') in {'trial_activation','subscription_activation'}:
                        if not callable(self.artifact_provider):raise ValueError('artifact_provider_missing')
                        sid=value.get('subscription_id')
                        subscription=self._action(job['customer_id'],'subscription',{'id':sid})['subscription']
                        for index,peer in enumerate(subscription.get('peers',[])[:5]):
                            for variant,kind in enumerate(('config','qr')):
                                part=1000+index*2+variant
                                with self.store.db(write=True) as db:
                                    previous=db.execute('SELECT 1 FROM commerce_public_notification_deliveries WHERE bot_id=? AND job_id=? AND chat_id=? AND part=?',(self._bot(),job['id'],target,part)).fetchone()
                                if previous:continue
                                body,_=self.artifact_provider(job['customer_id'],sid,peer['id'],kind)
                                self._send_artifact(self._token(),target,kind,body)
                                with self.store.db(write=True) as db:
                                    db.execute('INSERT OR IGNORE INTO commerce_public_notification_deliveries VALUES(?,?,?,?)',(self._bot(),job['id'],target,part))
            except Exception as error:
                self.service.finish_notification(job['id'],False,retry_after=getattr(error,'retry_after',None));continue
            self.service.finish_notification(job['id'],True);delivered+=1
        return delivered

    def poll_once(self, *, deliver_notifications=True):
        token=self._token();bot=token.split(':')[0]
        sent=self.flush_outbox()
        with self.store.db(write=True) as db:
            _ensure(db)
            db.execute('INSERT OR IGNORE INTO commerce_public_offsets(bot_id) VALUES(?)',(bot,))
            offset=db.execute('SELECT offset FROM commerce_public_offsets WHERE bot_id=?',(bot,)).fetchone()['offset']
        updates=self.transport.call(token,'getUpdates',{'offset':offset,'timeout':20,'limit':20,'allowed_updates':['message','callback_query']})
        if not isinstance(updates,list):raise ValueError('invalid_telegram_response')
        for update in updates:
            if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<offset:continue
            callback=update.get('callback_query') or {}
            if callback.get('id'):
                try:self.transport.call(token,'answerCallbackQuery',{'callback_query_id':str(callback['id'])[:256]},timeout=(2,3))
                except Exception:pass
            self.handle_update(update)
            with self.store.db(write=True) as db:db.execute('UPDATE commerce_public_offsets SET offset=MAX(offset,?) WHERE bot_id=?',(update['update_id']+1,bot))
            sent+=self.flush_outbox()
        return sent+(self.notify_pending() if deliver_notifications else 0)

    def run(self,stop=None):
        stop=stop or threading.Event()
        while not stop.is_set():
            try:self.poll_once()
            except Exception:stop.wait(10)
