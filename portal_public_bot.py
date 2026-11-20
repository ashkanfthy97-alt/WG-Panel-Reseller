"""Public customer bot. Polling is supervised by the existing panel process.

No inbound unauthenticated webhook is installed. Customer identity comes only
from private updates retrieved over the authenticated Telegram Bot API.
"""
import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from portal_reseller_bot import BotError, telegram_error_label

logger = logging.getLogger(__name__)


MENU=[('🛒 خرید اشتراک','shop'),('🧪 دریافت تست رایگان','trialinfo'),('📦 اشتراک‌های من','subscriptions'),
      ('💰 کیف پول','wallet'),('🎁 دعوت دوستان','referrals'),('📚 آموزش اتصال','guides'),
      ('📥 دانلود برنامه‌ها','downloads'),('🎧 پشتیبانی','support'),('🚀 ورود به پنل کاربری','miniapp')]
CATEGORIES={'economy':'🔥 طرح‌های اقتصادی','custom':'📊 اشتراک حجمی','unlimited':'♾ اشتراک نامحدود'}
PRICE_MENU='📋 طرح‌ها و قیمت‌ها'
MENU_ACTIONS={**dict(MENU),PRICE_MENU:'prices'}
MAIN_MENU_ROWS=[[MENU[0]],[MENU[1]],[MENU[2],MENU[3]],
                [(PRICE_MENU,'prices'),MENU[5]],[MENU[4],MENU[7]],[MENU[6]]]
UNIT_PRICES=('per_gb_toman','per_day_toman','per_device_toman')
CUSTOM_FIELDS=(('traffic_gb','حجم به گیگابایت',100000),('days','مدت به روز',3650),('devices','تعداد دستگاه',5))
DECIMAL_DIGITS=str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')
MENU_COMMAND=re.compile(r'^/(start|menu)(?:@[A-Za-z0-9_]+)?(?=\s|$)')
GUIDES='📚 راهنمای اتصال WireGuard\n۱. برنامهٔ رسمی WireGuard را نصب کنید.\n۲. فایل کانفیگ را وارد کنید یا کد QR اشتراک را اسکن کنید.\n۳. اتصال را روشن کنید.\nدر ویندوز و مک از Import tunnel from file و در اندروید و iOS از افزودن تونل استفاده کنید.'
DOWNLOADS='📥 برنامه‌های رسمی WireGuard برای اندروید، iOS، ویندوز و macOS:\nhttps://www.wireguard.com/install/'


def keyboard(rows):
    return {'inline_keyboard':[[{'text':label,'callback_data':value} for label,value in line] for line in rows]}


def _profile_text(value,limit):
    if not isinstance(value,str):
        return ''
    return ''.join(character for character in value[:limit] if character.isprintable())


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_updates (bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(bot_id,update_id))')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_states (bot_id TEXT NOT NULL, telegram_id INTEGER NOT NULL, value TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(bot_id,telegram_id))')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_offsets (bot_id TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_outbox (id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, customer_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_menu_messages (bot_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, outbox_id INTEGER NOT NULL, PRIMARY KEY(bot_id,chat_id))')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_receipt_targets (request_key TEXT PRIMARY KEY, payment_id INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_public_notification_deliveries (bot_id TEXT NOT NULL, job_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, part INTEGER NOT NULL, PRIMARY KEY(bot_id,job_id,chat_id,part))')


class PublicBot:
    def __init__(self,service,transport,base_url,token_provider,*,artifact_provider=None,receipt_image_provider=None,administrator_ids_provider=None,admin_provider=None,clock=None,bot_username=None):
        parsed=urlsplit(base_url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('public_miniapp_https_required')
        self.service=service;self.store=service.store;self.transport=transport;self.base_url=base_url.rstrip('/')
        self.token_provider=token_provider;self.artifact_provider=artifact_provider;self.clock=clock or time.time
        self.receipt_image_provider=receipt_image_provider
        self.administrator_ids_provider=administrator_ids_provider or admin_provider or (lambda:[])
        self.bot_username=bot_username if isinstance(bot_username,str) and re.fullmatch(r'[A-Za-z0-9_]{5,64}',bot_username) else None
        self.lock=threading.RLock()

    def _token(self):
        token=self.token_provider()
        if not isinstance(token,str) or not re.fullmatch(r'[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,120}',token):raise ValueError('public_bot_not_configured')
        return token

    def _bot(self):return self._token().split(':')[0]

    def main(self):
        markup=keyboard(MAIN_MENU_ROWS)
        markup['inline_keyboard'].append([self._miniapp_button()])
        return {'text':'به WG Portal خوش آمدید. گزینهٔ موردنظر را انتخاب کنید.','reply_markup':markup}

    def _miniapp_button(self):
        return {'text':MENU[-1][0],'web_app':{'url':self.base_url+'/telegram-app'}}

    def _reply_keyboard(self):
        rows=[[{'text':label} for label,_ in row] for row in MAIN_MENU_ROWS]
        rows.append([{'text':MENU[-1][0]}])
        return {'text':'از دکمه‌های پایین چت هم می‌توانید گزینه‌های اصلی را باز کنید.',
                'disable_notification':True,
                'reply_markup':{'keyboard':rows,'is_persistent':True,'resize_keyboard':True}}

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
        if not isinstance(callback,dict):return None
        message=callback.get('message') or update.get('message') or {}
        if not isinstance(message,dict):return None
        sender=(callback.get('from') if callback else message.get('from')) or {}
        chat=message.get('chat') or {}
        if not isinstance(sender,dict) or not isinstance(chat,dict):return None
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
            text='' if callback else str(message.get('text') or '').strip()
            message_id=message.get('message_id')
            author=message.get('from')
            callback_message_id=(message_id if callback and type(message_id) is int and message_id>0
                                 and isinstance(message.get('text'),str) and isinstance(author,dict) and author.get('is_bot') is True
                                 and str(author.get('id'))==bot else None)
            menu_command=MENU_COMMAND.match(text)
            argument=text[menu_command.end():].strip() if menu_command else ''
            referral=argument[4:] if menu_command and menu_command[1]=='start' and argument.startswith('ref_') else None
            customer=self.service.customer_from_telegram({
                'id':uid,
                'first_name':_profile_text(sender.get('first_name'),128),
                'username':_profile_text(sender.get('username'),64),
            },referral_code=referral)
            cid=customer['id'];key='public-bot-'+bot+'-'+str(update_id)
            state=self._state(bot,uid)
            data=callback.get('data')
            if callback_message_id is not None:
                with self.store.db() as db:
                    active=db.execute('SELECT message_id FROM commerce_public_menu_messages WHERE bot_id=? AND chat_id=?',(bot,uid)).fetchone()
                if active and callback_message_id!=active['message_id']:
                    return True
            attachment=not callback and ('photo' in message or 'document' in message)
            typed_value=not callback and text and text not in MENU_ACTIONS and state and state.get('step') in {
                'topup','custom_quantity','receipt','discount',
            } and not text.startswith('/')
            try:
                if attachment:
                    messages,new_state=self._receipt_attachment(cid,message,state,key)
                else:
                    messages,new_state=self._route(cid,text,data,state,key)
            except ValueError as error:
                # Domain error codes are mapped locally, never reflect internal
                # exception text, tokens, native responses or private keys.
                code=str(getattr(error,'code',''))
                friendly={'trial_already_used':'تست رایگان قبلاً دریافت شده است.',
                    'insufficient_balance':'موجودی کیف پول کافی نیست.',
                    'payment_unconfigured':'پرداخت هنوز توسط مدیر تنظیم نشده است.',
                    'payment_expired':'مهلت این پرداخت تمام شده است. سفارش تازه‌ای ثبت کنید.',
                    'payment_not_pending':'این پرداخت دیگر رسید جدید نمی‌پذیرد.',
                    'receipt_file_too_large':'حجم تصویر رسید بیشتر از ۵ مگابایت است. نسخهٔ کم‌حجم‌تر را بفرستید.',
                    'invalid_receipt':'یک تصویر سالم JPEG یا PNG از رسید بفرستید.',
                    'receipt_file_unavailable':'دریافت تصویر از تلگرام ممکن نشد. عکس را دوباره بفرستید.',
                    'telegram_rate_limited':'تلگرام موقتاً دریافت تصویر را محدود کرده است. کمی بعد دوباره امتحان کنید.',
                    'telegram_unavailable':'تلگرام موقتاً پاسخ نمی‌دهد. کمی بعد دوباره امتحان کنید.',
                    'telegram_connection_failed':'ارتباط با تلگرام برقرار نشد. کمی بعد دوباره امتحان کنید.',
                    'customer_suspended':'دسترسی حساب متوقف شده است؛ با پشتیبانی تماس بگیرید.'}.get(code,'عملیات انجام نشد؛ وضعیت درخواست را در پنل بررسی کنید و کمی بعد تلاش کنید.')
                retry_rows=None
                if isinstance(data,str) and data.startswith('receiptpick:') and state and state.get('step')=='receipt_select':
                    retry_rows=state.get('rows') if code in {'telegram_rate_limited','telegram_unavailable','telegram_connection_failed'} else None
                messages,new_state=[self._message(friendly,retry_rows)],state if attachment or retry_rows else None
            with self.store.db(write=True) as db:
                _ensure(db)
                if db.execute('SELECT 1 FROM commerce_public_updates WHERE bot_id=? AND update_id=? AND done=1',(bot,update_id)).fetchone():return True
                if new_state is None:db.execute('DELETE FROM commerce_public_states WHERE bot_id=? AND telegram_id=?',(bot,uid))
                else:db.execute('INSERT INTO commerce_public_states VALUES(?,?,?,?) ON CONFLICT(bot_id,telegram_id) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at',(bot,uid,json.dumps(new_state,ensure_ascii=False),int(self.clock())+600))
                if menu_command:
                    db.execute('INSERT INTO commerce_public_outbox(bot_id,customer_id,chat_id,payload) VALUES(?,?,?,?)',
                               (bot,cid,uid,json.dumps(self._reply_keyboard(),ensure_ascii=False)))
                for item in messages:
                    if 'text' in item:
                        encoded=item['text'].encode('utf-16-le')
                        screen=item['text'] if len(encoded)<=8000 else encoded[:7998].decode('utf-16-le',errors='ignore')+'…'
                        item=dict(item,text=screen,menu_message_id=callback_message_id)
                        if menu_command or attachment or typed_value:
                            item['fresh_menu']=True
                    db.execute('INSERT INTO commerce_public_outbox(bot_id,customer_id,chat_id,payload) VALUES(?,?,?,?)',(bot,cid,uid,json.dumps(item,ensure_ascii=False)))
                db.execute('INSERT INTO commerce_public_updates VALUES(?,?,1) ON CONFLICT(bot_id,update_id) DO UPDATE SET done=1',(bot,update_id))
                if attachment:
                    db.execute('DELETE FROM commerce_public_receipt_targets WHERE request_key=?',(key,))
            return True

    def _route(self,cid,text,data,state,key):
        if data is None and text in MENU_ACTIONS:
            data=MENU_ACTIONS[text]
        if data is not None and (not isinstance(data,str) or len(data)>64):return [self._message('گزینه معتبر نیست.')],None
        if MENU_COMMAND.match(text) or text=='/cancel' or data=='home':return [self.main()],None
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
        if data=='miniapp':
            screen=self._message('🚀 پنل کاربری\n\nبرای ورود امن از داخل تلگرام، دکمهٔ زیر را بزنید.')
            screen['reply_markup']['inline_keyboard'].insert(0,[self._miniapp_button()])
            return [screen],None
        if data=='shop':
            products=self._action(cid,'products').get('products',[])
            available={p.get('category') for p in products}
            rows=[[(label,'categoryshop:'+kind)] for kind,label in CATEGORIES.items() if kind in available]
            if not rows and products:rows=[[('📋 همهٔ طرح‌ها','prices')]]
            rows.append([('🧪 دریافت تست رایگان','trialinfo')])
            text=('🛒 خرید اشتراک WireGuard\n\nدستهٔ موردنظر را انتخاب کنید. طرح ثابت مستقیم به پرداخت می‌رسد و در طرح دلخواه مقدارها را خودتان وارد می‌کنید.' if products
                  else '🛒 خرید اشتراک\n\nدر حال حاضر طرحی برای فروش فعال نیست.')
            return [self._message(text,rows)],None
        category=re.fullmatch(r'categoryshop:(economy|custom|unlimited)',data or '')
        if data=='prices' or category:
            products=self._action(cid,'products').get('products',[])
            if category:products=[p for p in products if p.get('category')==category[1]]
            rows=[]
            for product in products[:80]:
                pricing=product.get('pricing') or {}
                price=('بر اساس انتخاب شما' if product.get('category')=='custom' and all(field in pricing for field in UNIT_PRICES)
                       else f'{product.get("price_toman",0):,} تومان')
                rows.append([(str(product.get('name','اشتراک'))[:50]+' · '+price,'product:'+str(product['id']))])
            rows.append([('⬅️ دسته‌بندی طرح‌ها','shop')])
            title=CATEGORIES[category[1]] if category else PRICE_MENU
            detail=('برای دیدن اطلاعات پرداخت، طرح ثابت را انتخاب کنید. طرح دلخواه با مقدارهای انتخابی شما محاسبه می‌شود.'
                    if products else 'در این دسته فعلاً طرح فعالی برای خرید وجود ندارد.')
            return [self._message(title+'\n\n'+detail,rows)],None
        match=re.fullmatch(r'product:([1-9][0-9]*)',data or '')
        if match:
            product=next((p for p in self._action(cid,'products').get('products',[]) if p['id']==int(match[1])),None)
            if product is None:return [self._message('طرح در دسترس نیست.')],None
            if product.get('category')!='custom':
                if state and state.get('step')=='fixed_payment' and state['product_id']==product['id']:
                    existing=self._action(cid,'payment',{'id':state['payment_id']})['payment']
                    if existing['status']=='pending_payment' and existing['expires_at']>int(self.clock()):
                        return self._payment_messages(state['order'],existing),state
                result=self._action(cid,'checkout',{'product_id':product['id']},key)
                payment=result['payment']
                return self._payment_messages(result['order'],payment),{
                    'step':'fixed_payment','product_id':product['id'],'payment_id':payment['id'],'order':{'id':result['order']['id']},
                }
            pricing=product.get('pricing') or {}
            if all(field in pricing for field in UNIT_PRICES):
                custom={'step':'custom_quantity','index':0,'selection':{'product_id':product['id']}}
                return [self._custom_prompt(custom,pricing)],custom
            selection={'product_id':product['id'],'traffic_gb':product.get('traffic_gb'),'days':product['days'],'devices':product['devices']}
            state={'step':'traffic','selection':selection,'product':product}
            options=list(dict.fromkeys([product.get('traffic_gb')]+pricing.get('traffic_options',[])))
            rows=[[(('نامحدود' if amount is None else str(amount)+' گیگابایت'),'traffic:'+str(amount))] for amount in options]
            category=product.get('category')
            rows.append([('⬅️ طرح‌ها','categoryshop:'+category if category in CATEGORIES else 'prices')])
            title=str(product.get('name','اشتراک'))[:100]
            return [self._message('📊 حجم اشتراک '+title+'\n\nحجم تعیین می‌کند در دورهٔ اشتراک چقدر ترافیک در اختیار دارید. یکی از گزینه‌های این طرح را انتخاب کنید.',rows)],state
        if state and state.get('step')=='custom_quantity' and not data:
            field,_,limit=CUSTOM_FIELDS[state['index']]
            normalized=text.translate(DECIMAL_DIGITS)
            if not re.fullmatch(r'[0-9]{1,6}',normalized) or not 1<=int(normalized)<=limit:
                return [self._custom_prompt(state,error=True)],state
            selection=dict(state['selection'],**{field:int(normalized)})
            index=state['index']+1
            if index<len(CUSTOM_FIELDS):
                next_state=dict(state,index=index,selection=selection)
                return [self._custom_prompt(next_state)],next_state
            return self._quote_screen(cid,selection,key,back=('⬅️ انتخاب دوبارهٔ طرح','categoryshop:custom'))
        match=re.fullmatch(r'traffic:(None|[1-9][0-9]*)',data or '')
        if match and state and state.get('step')=='traffic':
            traffic=None if match[1]=='None' else int(match[1]);product=state['product'];pricing=product.get('pricing') or {}
            if traffic not in [product.get('traffic_gb')]+pricing.get('traffic_options',[]):raise ValueError('invalid_traffic')
            state['selection']['traffic_gb']=traffic;state['step']='duration'
            options=list(dict.fromkeys([product['days']]+pricing.get('duration_options',[])))
            rows=[[(str(days)+' روز','duration:'+str(days))] for days in options]
            rows.append([('⬅️ انتخاب حجم','back:traffic')])
            return [self._message('📅 مدت اشتراک\n\nتعداد روزهایی را انتخاب کنید که می‌خواهید سرویس پس از فعال‌سازی اعتبار داشته باشد.',rows)],state
        match=re.fullmatch(r'duration:([1-9][0-9]*)',data or '')
        if match and state and state.get('step')=='duration':
            days=int(match[1]);product=state['product'];pricing=product.get('pricing') or {}
            if days not in [product['days']]+pricing.get('duration_options',[]):raise ValueError('invalid_duration')
            state['selection']['days']=days;state['step']='devices'
            options=list(dict.fromkeys([product['devices']]+pricing.get('device_options',[])))
            rows=[[(str(count)+' دستگاه','devices:'+str(count))] for count in options]
            rows.append([('⬅️ انتخاب مدت','back:duration')])
            return [self._message('📱 تعداد دستگاه‌ها\n\nبرای هر دستگاه یک کانفیگ جدا آماده می‌شود. بعد از این انتخاب، مبلغ نهایی را در پیش‌فاکتور می‌بینید.',rows)],state
        match=re.fullmatch(r'devices:([1-5])',data or '')
        if match and state and state.get('step')=='devices':
            selection=dict(state['selection'],devices=int(match[1]))
            return self._quote_screen(cid,selection,key,state=state,back=('⬅️ تعداد دستگاه','back:devices'))
        if data=='discount' and state and state.get('step')=='invoice':
            return [self._message('🎟 کد تخفیف\n\nکد را در همین چت بفرستید تا مبلغ پیش‌فاکتور دوباره محاسبه شود.')],dict(state,step='discount')
        if state and state.get('step')=='discount' and not data:
            quote=self._action(cid,'quote',dict(state['selection'],discount_code=text[:64]),key)['quote']
            return [self._message('مبلغ پس از تخفیف: '+format(quote['total_toman'],',')+' تومان',[[('💳 کارت‌به‌کارت','buy:card'),('💰 کیف پول','buy:wallet')]])],dict(state,step='invoice',quote_id=str(quote['id']))
        if data in {'buy:card','buy:wallet'} and state and state.get('step')=='invoice':
            result=self._action(cid,'orders',{'quote_id':state['quote_id'],'payment_method':'card' if data=='buy:card' else 'wallet'},key)
            order=result['order'];payment=result.get('payment') or {}
            if payment:
                return self._payment_messages(order,payment),None
            return [self._message('✅ سفارش '+str(order['id'])+' ثبت شد. وضعیت فعال‌سازی را در اشتراک‌های من ببینید.',[[('📦 اشتراک‌های من','subscriptions')]])],None
        if data=='trialinfo':
            return [self._message('🧪 تست رایگان\n\nبا تست رایگان می‌توانید اتصال WireGuard را پیش از خرید امتحان کنید. هر حساب یک بار می‌تواند تست بگیرد. پس از ثبت، وضعیت و کانفیگ آماده‌شده در «اشتراک‌های من» نمایش داده می‌شود.',
                                  [[('🧪 ثبت درخواست تست','trial')]])],None
        if data=='trial':
            result=self._action(cid,'trial',{},key)
            subscription=result.get('subscription') or result.get('order') or {}
            return [self._message('🧪 درخواست تست ثبت شد. وضعیت: '+str(subscription.get('status','در حال آماده‌سازی')),[[('📦 اشتراک‌های من','subscriptions')]])],None
        if data=='wallet':
            wallet=self._action(cid,'wallet')
            lines=['💰 کیف پول','موجودی: '+format(wallet.get('balance_toman',0),',')+' تومان',
                   'می‌توانید موجودی را برای خرید استفاده کنید. شارژ کارت‌به‌کارت پس از بررسی رسید به موجودی اضافه می‌شود.']
            for row in wallet.get('transactions',[])[:10]:lines.append(str(row.get('amount_toman',row.get('amount',0)))+' تومان · '+str(row.get('kind','تراکنش')))
            return [self._message('\n'.join(lines),[[('➕ شارژ کیف پول','topup')]])],None
        if data=='topup':
            return [self._message('➕ شارژ کیف پول\n\nمبلغ را به تومان و فقط با عدد در همین چت بفرستید تا اطلاعات واریز نمایش داده شود.',
                                  [[('⬅️ کیف پول','wallet')]])],{'step':'topup'}
        if state and state.get('step')=='topup' and not data:
            normalized=text.translate(DECIMAL_DIGITS)
            if not re.fullmatch(r'[0-9]{1,10}',normalized):return [self._message('مبلغ معتبر به تومان وارد کنید.')],state
            result=self._action(cid,'topup',{'amount_toman':int(normalized)},key)
            return self._payment_messages(result.get('order',{}),result['payment']),None
        match=re.fullmatch(r'receipt:([1-9][0-9]*)',data or '')
        if match:
            pid=int(match[1])
            self._action(cid,'payment',{'id':pid})
            return [self._message('📷 عکس رسید واریز را در همین چت بفرستید. شمارهٔ پیگیری را می‌توانید جداگانه یا در توضیح عکس بنویسید.')],{'step':'receipt','payment_id':pid}
        match=re.fullmatch(r'receiptpick:([1-9][0-9]*)',data or '')
        if match and state and state.get('step')=='receipt_select':
            pid=int(match[1])
            if pid not in state['payment_ids']:
                return [self._message('پرداخت را از فهرست پرداخت‌های خودتان انتخاب کنید.',state['rows'])],state
            return self._submit_chat_image(cid,pid,state['file_id'],state['caption'],key)
        if state and state.get('step')=='receipt' and not data and text:
            self._action(cid,'submit_receipt',{'payment_id':state['payment_id'],'transaction_text':text[:1000]},key)
            return [self._message('⏳ اطلاعات پرداخت برای بررسی مدیر ثبت شد. اگر عکس رسید دارید، آن را در همین چت بفرستید.')],state
        if data=='subscriptions':
            subs=self._action(cid,'subscriptions').get('subscriptions',[])
            rows=[[(str(s.get('product_name',s.get('name','اشتراک')))[:35]+' · '+str(s['id']),'subscription:'+str(s['id']))] for s in subs[:80]]
            text=('📦 اشتراک‌های من\n\nبرای بررسی وضعیت، دانلود کانفیگ یا تمدید، اشتراک را انتخاب کنید.' if rows
                  else '📦 اشتراک‌های من\n\nهنوز اشتراکی در حساب شما ثبت نشده است. از منوی اصلی می‌توانید طرح‌های فعال یا تست رایگان را ببینید.')
            return [self._message(text,rows)],None
        match=re.fullmatch(r'subscription:([1-9][0-9]*)',data or '')
        if match:
            sid=int(match[1]);sub=self._action(cid,'subscription',{'id':sid})['subscription'];rows=[]
            for peer in sub.get('peers',[]):
                rows.append([('📄 فایل دستگاه '+str(peer['id']),'config:'+str(sid)+':'+str(peer['id'])),('▦ کد QR','qr:'+str(sid)+':'+str(peer['id']))])
            rows.append([('🔄 تمدید','renew:'+str(sid)),('⬆️ ارتقا','upgrade:'+str(sid))])
            return [self._message('📦 اشتراک '+str(sid)+'\nوضعیت: '+str(sub.get('status',''))+'\nمصرف ثبت‌شده: '+str(sub.get('used_bytes','نامشخص'))+' بایت\nزمان باقی‌مانده: '+str(sub.get('remaining_seconds','نامشخص'))+' ثانیه\n\nبرای اتصال، فایل دستگاه یا کد QR را انتخاب کنید.',rows)],None
        match=re.fullmatch(r'(config|qr):([1-9][0-9]*):([1-9][0-9]*)',data or '')
        if match:
            sid,pid=int(match[2]),int(match[3])
            sub=self._action(cid,'subscription',{'id':sid})['subscription']
            if pid not in {p.get('id') for p in sub.get('peers',[])}:raise ValueError('peer_not_owned')
            return [{'artifact':{'subscription_id':sid,'peer_id':pid,'kind':match[1]}}],None
        match=re.fullmatch(r'(renew|upgrade):([1-9][0-9]*)',data or '')
        if match:
            products=self._action(cid,'products').get('products',[])
            caption=('🔄 تمدید اشتراک\n\nطرحی را انتخاب کنید تا هزینهٔ تمدید همین اشتراک را ببینید.'
                     if match[1]=='renew' else '⬆️ ارتقای اشتراک\n\nطرح جدید را انتخاب کنید تا هزینهٔ ارتقای همین اشتراک را ببینید.')
            return [self._message(caption,[[(str(product['name'])[:40],match[1]+'product:'+match[2]+':'+str(product['id']))] for product in products[:80]])],None
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
            return [self._message('🎁 دعوت دوستان\n\nلینک اختصاصی را برای دوستانتان بفرستید. پاداش خریدهای واجد شرایط بعد از تأیید پرداخت ثبت می‌شود.\n\n'+link+'\nدعوت‌ها: '+str(result.get('referral_count',0))+'\nپاداش تأییدشده: '+format(result.get('earnings_toman',0),',')+' تومان')],None
        if data in {'guides','downloads'}:return [self._message(GUIDES if data=='guides' else DOWNLOADS)],None
        if data=='support':
            tickets=self._action(cid,'tickets').get('tickets',[])
            rows=[[(str(t.get('category','پشتیبانی'))+' · '+str(t['id']),'ticket:'+str(t['id']))] for t in tickets[:20]]
            rows.insert(0,[('✍️ تیکت جدید','ticketnew')])
            return [self._message('🎧 پشتیبانی\n\nبرای پیگیری درخواست، تیکت را باز کنید. برای مطرح‌کردن موضوع تازه، «تیکت جدید» را بزنید.',rows)],None
        if data=='ticketnew':
            return [self._message('✍️ تیکت جدید\n\nنزدیک‌ترین موضوع به درخواستتان را انتخاب کنید. مرحلهٔ بعد متن درخواست را می‌فرستید.',
                                  [[('پرداخت','category:payment'),('اتصال','category:connection')],[('اشتراک','category:subscription'),('کیف پول','category:wallet')],[('سایر','category:other')]])],None
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
            history='\n\n'.join(str(row.get('body','')) for row in rows[-12:])
            if len(history)>1750:history='…\n'+history[-1750:]
            text='🎧 گفت‌وگوی تیکت '+str(tid)+'\n\n'+history+'\n\nبرای پاسخ، همین‌جا پیام بفرستید. برای دیدن کل گفت‌وگو، از پنل کاربری استفاده کنید.'
            return [self._message(text,[[('⬅️ تیکت‌ها','support')]])],{'step':'ticket_reply','id':tid}
        if state and state.get('step')=='ticket_reply' and not data:
            self._action(cid,'reply_ticket',{'id':state['id'],'body':text[:4000]},key)
            return [self._message('✅ پاسخ شما ثبت شد.')],None
        return [self.main()],None

    def _custom_prompt(self,state,pricing=None,error=False):
        _,label,limit=CUSTOM_FIELDS[state['index']]
        prompt=f'{label} را به‌صورت عدد از ۱ تا {limit:,} در همین چت بفرستید.'
        if pricing:
            prompt=('📊 طرح دلخواه\n\n'
                    f'هر گیگابایت {pricing["per_gb_toman"]:,} تومان، هر روز {pricing["per_day_toman"]:,} تومان، '
                    f'هر دستگاه {pricing["per_device_toman"]:,} تومان.\n\n'+prompt)
        elif error:
            prompt='عدد واردشده معتبر نیست. '+prompt
        return self._message(prompt,[[('⬅️ طرح‌های دلخواه','categoryshop:custom')]])

    def _quote_screen(self,cid,selection,key,*,back,state=None):
        quote=self._action(cid,'quote',selection,key)['quote']
        rows=[[('💳 کارت‌به‌کارت','buy:card'),('💰 کیف پول','buy:wallet')],
              [('🎟 کد تخفیف','discount')],[back]]
        traffic=quote.get('traffic_gb')
        volume='نامحدود' if traffic is None else f'{traffic} گیگابایت'
        screen=self._message(
            f'🧾 پیش‌فاکتور\nطرح: {quote["product_name"]}\nحجم: {volume}\nمدت: {quote["days"]} روز\n'
            f'دستگاه: {quote["devices"]}\nقیمت پایه: {quote["base_toman"]:,} تومان\n'
            f'تخفیف: {quote["discount_toman"]:,} تومان\nمبلغ نهایی: {quote["total_toman"]:,} تومان\nپرداخت را انتخاب کنید.',rows)
        return [screen],dict(state or {},step='invoice',quote_id=str(quote['id']),selection=selection)

    def _payment_messages(self,order,payment):
        destination=payment.get('payment_destination') or order.get('payment_destination') or {}
        card=destination.get('card_number','');name=destination.get('cardholder_name',destination.get('holder_name',''))
        return [self._message('💳 پرداخت کارت‌به‌کارت\nسفارش: '+str(order.get('id',payment.get('id','')))+'\nمبلغ: '+format(payment.get('amount_toman',order.get('total_toman',0)),',')+' تومان\nشماره کارت: '+str(card)+'\nصاحب کارت: '+str(name)+'\nبانک: '+str(destination.get('bank_name',''))+'\n'+str(destination.get('instructions',''))+'\nبرای پرداخت و ثبت رسید تا ۱۰ دقیقه فرصت دارید. عکس رسید را در همین چت بفرستید؛ پرداخت پس از بررسی مدیر تأیید می‌شود.',[[('📤 ثبت رسید','receipt:'+str(payment['id']))]])]

    def _receipt_attachment(self,cid,message,state,key):
        photos=message.get('photo')
        image=None
        if isinstance(photos,list):
            image=next((item for item in reversed(photos) if isinstance(item,dict) and item.get('file_id')),None)
        else:
            document=message.get('document')
            if isinstance(document,dict):
                mime=document.get('mime_type')
                name=document.get('file_name')
                if mime in ('image/jpeg','image/png') or (
                    mime in (None,'application/octet-stream') and isinstance(name,str)
                    and name.lower().endswith(('.jpg','.jpeg','.png'))
                ):
                    image=document
        file_id=image.get('file_id') if image else None
        if not isinstance(file_id,str) or not 1<=len(file_id)<=1024:
            return [self._message('عکس رسید را به صورت تصویر یا فایل JPEG یا PNG بفرستید.')],state
        size=image.get('file_size')
        if type(size) is int and size>5*1024*1024:
            return [self._message('حجم تصویر رسید باید حداکثر ۵ مگابایت باشد.')],state
        caption=message.get('caption')
        caption=''.join(char for char in caption[:1000] if ord(char)>=32 or char in '\n\r\t').strip() if isinstance(caption,str) else ''
        with self.store.db() as db:
            target=db.execute('SELECT payment_id FROM commerce_public_receipt_targets WHERE request_key=?',(key,)).fetchone()
        if target:
            return self._submit_chat_image(cid,target['payment_id'],file_id,caption,key)
        if state and state.get('step')=='receipt':
            pid=state['payment_id']
        else:
            payments=self._action(cid,'receipt_payments').get('payments',[])
            if not payments:
                return [self._message('پرداخت بازی بدون عکس رسید پیدا نشد. وضعیت سفارش‌ها را در پنل کاربری ببینید.')],state
            pid=payments[0]['id']
        with self.store.db(write=True) as db:
            db.execute('INSERT OR IGNORE INTO commerce_public_receipt_targets VALUES(?,?)',(key,pid))
            pid=db.execute('SELECT payment_id FROM commerce_public_receipt_targets WHERE request_key=?',(key,)).fetchone()['payment_id']
        return self._submit_chat_image(cid,pid,file_id,caption,key)

    def _submit_chat_image(self,cid,pid,file_id,caption,key):
        payment=self._action(cid,'payment',{'id':pid})['payment']
        if payment['status'] not in {'pending_payment','pending_review'}:
            return [self._message('این پرداخت دیگر رسید نمی‌پذیرد. وضعیت سفارش را در پنل کاربری بررسی کنید.')],None
        if not callable(self.receipt_image_provider):
            return [self._message('دریافت عکس در بات موقتاً در دسترس نیست. می‌توانید رسید را در پنل کاربری بارگذاری کنید.')],None
        body=self.transport.download_file(self._token(),file_id)
        reference=self.receipt_image_provider(body,f'telegram:{cid}:{pid}:{key}')
        payload={'payment_id':pid,'receipt_reference':reference}
        if caption:
            payload['transaction_text']=caption
        self._action(cid,'submit_receipt',payload,key)
        identifier=('سفارش '+str(payment['order_id']) if payment.get('order_id') else 'پرداخت '+str(pid))
        return [self._message('✅ عکس رسید '+identifier+' ثبت شد و در انتظار بررسی مدیر است.',
                              [[('ثبت شمارهٔ پیگیری','receipt:'+str(pid))]])],None

    def _send_artifact(self,token,chat_id,kind,body):
        payload={'chat_id':chat_id,'filename':'wireguard.conf' if kind=='config' else 'wireguard-qr.png'}
        try:self.transport.call(token,'sendDocument' if kind=='config' else 'sendPhoto',payload,**({'document':body} if kind=='config' else {'photo':body}))
        except Exception as error:
            # Retry as a document only after Telegram definitely rejected the
            # photo. Ambiguous network errors stay in the delivery queue.
            if kind!='qr' or getattr(error,'code','')!='telegram_bad_request':raise
            self.transport.call(token,'sendDocument',payload,document=body)

    def flush_outbox(self,limit=20):
        with self.lock:
            return self._flush_outbox(limit)

    def _flush_outbox(self,limit):
        bot=self._bot();token=self._token();now=int(self.clock());sent=0
        with self.store.db(write=True) as db:
            _ensure(db)
            rows=db.execute('SELECT * FROM commerce_public_outbox WHERE bot_id=? AND sent=0 AND next_at<=? AND locked_until<=? ORDER BY id LIMIT ?',(bot,now,now,max(1,min(100,limit)))).fetchall()
        for row in rows:
            with self.store.db(write=True) as db:
                if db.execute('UPDATE commerce_public_outbox SET locked_until=? WHERE id=? AND sent=0 AND locked_until<=?',(now+60,row['id'],now)).rowcount!=1:continue
            try:
                payload=json.loads(row['payload'])
                menu_message_id=None
                previous_menu_id=None
                rendered=True
                if 'artifact' in payload:
                    artifact=payload['artifact']
                    if not callable(self.artifact_provider):raise ValueError('artifact_provider_missing')
                    body,_=self.artifact_provider(row['customer_id'],artifact['subscription_id'],artifact['peer_id'],artifact['kind'])
                    self._send_artifact(token,row['chat_id'],artifact['kind'],body)
                elif 'menu_message_id' in payload:
                    menu_message_id,previous_menu_id=self._deliver_menu(token,bot,row,payload)
                    rendered=menu_message_id is not None
                else:
                    if payload.get('reply_markup',{}).get('remove_keyboard'):
                        payload=self._reply_keyboard()
                    self.transport.call(token,'sendMessage',{'chat_id':row['chat_id'],'disable_web_page_preview':True,**payload})
            except Exception as error:
                attempts=row['attempts']+1;delay=max(min(3600,2**min(attempts,11)),int(getattr(error,'retry_after',0) or 0))
                if attempts in {1,12}:
                    logger.warning('Telegram commerce reply delivery failed (%s, attempt %d)',telegram_error_label(error),attempts)
                with self.store.db(write=True) as db:
                    db.execute('UPDATE commerce_public_outbox SET attempts=?,next_at=?,locked_until=0,sent=? WHERE id=?',(attempts,now+delay,-1 if attempts>=12 else 0,row['id']))
                continue
            with self.store.db(write=True) as db:
                if menu_message_id is not None:
                    db.execute('''INSERT INTO commerce_public_menu_messages VALUES(?,?,?,?)
                        ON CONFLICT(bot_id,chat_id) DO UPDATE SET message_id=excluded.message_id,outbox_id=excluded.outbox_id
                        WHERE excluded.outbox_id>commerce_public_menu_messages.outbox_id''',
                        (bot,row['chat_id'],menu_message_id,row['id']))
                db.execute('UPDATE commerce_public_outbox SET sent=1,locked_until=0 WHERE id=?',(row['id'],))
            if previous_menu_id is not None:
                try:
                    self.transport.call(token,'editMessageReplyMarkup',{
                        'chat_id':row['chat_id'],'message_id':previous_menu_id,'reply_markup':{'inline_keyboard':[]},
                    },timeout=(2,3))
                except Exception as error:
                    logger.info('Telegram commerce previous menu cleanup (%s)',telegram_error_label(error))
            sent+=rendered
        return sent

    def _deliver_menu(self,token,bot,row,payload):
        callback_message_id=payload.pop('menu_message_id')
        fresh_menu=payload.pop('fresh_menu',False)
        with self.store.db() as db:
            menu=db.execute('SELECT message_id,outbox_id FROM commerce_public_menu_messages WHERE bot_id=? AND chat_id=?',
                            (bot,row['chat_id'])).fetchone()
        if menu and menu['outbox_id']>=row['id']:
            return None,None
        message_id=menu['message_id'] if menu else callback_message_id
        response={'chat_id':row['chat_id'],'link_preview_options':{'is_disabled':True},**payload}
        if not fresh_menu and message_id is not None:
            try:
                self._write_menu(token,'editMessageText',dict(response,message_id=message_id))
                return message_id,None
            except Exception as error:
                if getattr(error,'code','')!='telegram_message_uneditable':raise
        result=self._write_menu(token,'sendMessage',response)
        new_id=result.get('message_id') if isinstance(result,dict) else None
        if type(new_id) is not int or new_id<=0:
            raise ValueError('telegram_message_id_missing')
        return new_id,message_id if new_id!=message_id else None

    def _write_menu(self,token,method,response):
        try:
            return self.transport.call(token,method,response)
        except BotError as error:
            if error.code!='telegram_miniapp_button_rejected':
                raise
            rows=response['reply_markup']['inline_keyboard']
            fallback=[[button for button in row if 'web_app' not in button] for row in rows]
            if rows==fallback:
                raise
            logger.warning('Telegram commerce Mini App button rejected; delivering the public menu')
            return self.transport.call(token,method,dict(response,reply_markup={'inline_keyboard':[row for row in fallback if row]}))

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
            if isinstance(callback,dict) and isinstance(callback.get('id'),str):
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
