"""Customer commerce extension. No customer request inherits native authority."""
from functools import wraps
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request, send_file
from werkzeug.test import Client
from werkzeug.wrappers import Response as NativeResponse

COOKIE = 'wgportal_customer'
_SAFE_REQUEST = re.compile(r'[A-Za-z0-9_.:-]{16,80}')


class CommerceHTTPError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def install_commerce(app, csrf):
    if 'telegram_commerce' in app.blueprints:
        return
    if app.testing and app.config.get('COMMERCE_STORE_FACTORY'):
        Store = app.config['COMMERCE_STORE_FACTORY']
        Service = app.config['COMMERCE_SERVICE_FACTORY']
        Auth = app.config['COMMERCE_AUTH_FACTORY']
        Native = app.config['COMMERCE_NATIVE_FACTORY']
    else:
        from portal_commerce_store import Store
        from portal_commerce_service import CommerceService as Service
        from portal_commerce_auth import Auth
        from portal_commerce_native import NativeAdapter as Native
    private = Path(app.instance_path) / 'telegram-commerce'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(private / 'commerce.sqlite3')
    store.initialize()
    app.extensions['portal_commerce_store'] = store
    adapter = Native(app, store)
    service = Service(store, native_adapter=adapter)
    app.extensions['portal_commerce_service'] = service
    runtime = RuntimeConfig(app, store)
    auth = Auth(store, runtime.token, service)
    app.extensions['portal_commerce_auth'] = auth
    bp = Blueprint('telegram_commerce', __name__)

    def owner():
        if app.testing and callable(app.config.get('COMMERCE_OWNER_CHECK')):
            return app.config['COMMERCE_OWNER_CHECK']()
        names=(app.session_interface.get_cookie_name(app),app.config.get('REMEMBER_COOKIE_NAME','remember_token'))
        cookies = '; '.join(name+'='+request.cookies[name] for name in names if request.cookies.get(name))
        if not cookies:
            return None
        # Fresh context avoids cached Flask-Login identity from other roles.
        with app.app_context():
            response = Client(app, NativeResponse, use_cookies=False).get('/api/admin',
                headers={'Cookie': cookies}, base_url=request.host_url)
            try:
                value = response.get_json(silent=True)
                return value.get('username') if response.status_code == 200 and isinstance(value, dict) else None
            finally:
                response.close()

    def valid_origin():
        origin = urlsplit(request.headers.get('Origin') or '')
        return origin.scheme == request.scheme and origin.netloc.lower() == request.host.lower()

    def customer():
        value = auth.authenticate(request.cookies.get(COOKIE, ''))
        if not value:
            raise CommerceHTTPError('login_required', 'از داخل بات وارد پنل کاربری شوید.', 401)
        return value

    def owner_csrf():
        # Native session holds only a CSRF secret; it grants no new role.
        from flask import session
        if not session.get('commerce_csrf'):
            session['commerce_csrf'] = secrets.token_urlsafe(32)
        return session['commerce_csrf']

    @bp.before_request
    def boundary():
        if not request.is_secure and not app.testing:
            raise CommerceHTTPError('https_required', 'این بخش باید با HTTPS باز شود.', 400)
        request.max_content_length = 6 * 1024 * 1024
        if request.path.startswith('/telegram-commerce'):
            g.commerce_owner = owner()
            if not g.commerce_owner:
                raise CommerceHTTPError('admin_required', 'ورود مدیر اصلی لازم است.', 401)
            expected = owner_csrf()
        elif request.path.startswith('/telegram-app/api/') and request.path != '/telegram-app/api/auth':
            g.commerce_customer = customer()
            expected = g.commerce_customer['csrf']
        else:
            expected = None
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not valid_origin():
                raise CommerceHTTPError('invalid_origin', 'مبدأ درخواست معتبر نیست.', 403)
            if expected and not hmac.compare_digest(str(expected), request.headers.get('X-Commerce-CSRF', '')):
                raise CommerceHTTPError('invalid_csrf', 'صفحه را تازه کنید و دوباره تلاش کنید.', 403)
            if request.path != '/telegram-app/api/auth' and not _SAFE_REQUEST.fullmatch(request.headers.get('Idempotency-Key', '')):
                raise CommerceHTTPError('idempotency_required', 'شناسهٔ یکتای درخواست لازم است.')
            if request.mimetype == 'application/json' and request.content_length and request.content_length > 65536:
                raise CommerceHTTPError('request_too_large', 'درخواست بیش از حد بزرگ است.', 413)
            if request.path.startswith('/telegram-app/api/') and request.path!='/telegram-app/api/auth' and hasattr(store,'db'):
                bucket=str(g.commerce_customer['customer_id'])+('upload' if request.mimetype=='multipart/form-data' else 'write')
                now=int(time.time());maximum=10 if request.mimetype=='multipart/form-data' else 90
                with store.db(write=True) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS commerce_write_limits (key TEXT PRIMARY KEY, window INTEGER NOT NULL, attempts INTEGER NOT NULL)')
                    row=db.execute('SELECT * FROM commerce_write_limits WHERE key=?',(bucket,)).fetchone()
                    count=row['attempts']+1 if row and row['window']>now-60 else 1
                    start=row['window'] if row and row['window']>now-60 else now
                    db.execute('INSERT OR REPLACE INTO commerce_write_limits VALUES(?,?,?)',(bucket,start,count))
                if count>maximum:
                    raise CommerceHTTPError('rate_limited','تعداد درخواست‌ها زیاد است؛ کمی بعد دوباره تلاش کنید.',429)

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=getattr(error, 'code', 'invalid_request'),
                       message=getattr(error, 'message', 'اطلاعات درخواست معتبر نیست.'),
                       retryable=False), getattr(error, 'status', 400)

    @bp.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if request.path.startswith('/telegram-app'):
            response.headers.pop('X-Frame-Options', None)
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; base-uri 'none'; object-src 'none'; "
                "script-src 'self' https://telegram.org; connect-src 'self'; "
                "style-src 'self'; font-src 'self'; img-src 'self' data: blob:; "
                "frame-ancestors https://web.telegram.org https://*.telegram.org")
        return response

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise CommerceHTTPError('invalid_json', 'اطلاعات فرم معتبر نیست.')
        return value

    def ticket_data(*,admin=False):
        if request.mimetype != 'multipart/form-data':
            value=data()
            if set(value)-({'body','category','close'} if admin else {'body','category'}):
                raise CommerceHTTPError('invalid_ticket','اطلاعات تیکت معتبر نیست.')
            return value
        value={key:request.form[key] for key in request.form}
        if set(value)-({'body','category','close'} if admin else {'body','category'}):
            raise CommerceHTTPError('invalid_ticket','اطلاعات تیکت معتبر نیست.')
        if request.files.get('file'):
            value['attachment_reference']=save_image(request.files['file'],private)
        return value

    def invoke(action, payload=None, *, admin=False):
        key = request.headers.get('Idempotency-Key')
        if admin:
            result = service.admin_action(g.commerce_owner, action, payload or {}, key)
            result = dict(result or {})
            if action=='jobs':
                result['jobs']=[{**item,'kind':'provision','last_error':item.get('error')} for item in result.get('jobs',[])]+[
                    {**item,'kind':'notification'} for item in result.get('notifications',[])]
            result['csrf'] = owner_csrf()
        else:
            result = service.customer_action(g.commerce_customer['customer_id'], action, payload or {}, key)
            if action=='referrals':
                username=runtime.public().get('bot_username')
                if username and re.fullmatch(r'[A-Za-z0-9_]{5,64}',username):
                    result['link']='https://t.me/'+username+'?start=ref_'+result['referral_code']
        return jsonify(result)

    @bp.get('/telegram-app')
    def miniapp():
        return render_template('telegram-mini-app.html')

    @bp.get('/telegram-commerce')
    def administration():
        return render_template('telegram-commerce.html', commerce_csrf=owner_csrf())

    @bp.post('/telegram-app/api/auth')
    def login():
        # Persistent admission limit, keyed by a digest instead of raw IP.
        if hasattr(store, 'db'):
            key = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
            now = int(time.time())
            with store.db(write=True) as db:
                db.execute('CREATE TABLE IF NOT EXISTS commerce_auth_limits (key TEXT PRIMARY KEY, window INTEGER NOT NULL, attempts INTEGER NOT NULL)')
                row = db.execute('SELECT * FROM commerce_auth_limits WHERE key=?', (key,)).fetchone()
                attempts = row['attempts']+1 if row and row['window'] > now-300 else 1
                window = row['window'] if row and row['window'] > now-300 else now
                db.execute('INSERT OR REPLACE INTO commerce_auth_limits VALUES(?,?,?)', (key, window, attempts))
            if attempts > 30:
                raise CommerceHTTPError('rate_limited', 'کمی صبر کنید و دوباره وارد شوید.', 429)
        value = data()
        if set(value) != {'init_data'}:
            raise CommerceHTTPError('invalid_auth', 'اطلاعات ورود معتبر نیست.')
        result = dict(auth.login(value['init_data']))
        token = result.pop('token')
        response = jsonify(result)
        response.set_cookie(COOKIE, token, path='/telegram-app', secure=request.is_secure,
                            httponly=True, samesite='None' if request.is_secure else 'Lax', max_age=3600)
        return response

    @bp.get('/telegram-app/api/session')
    def customer_session():
        value = g.commerce_customer
        return jsonify(customer=value.get('customer', {'id':value['customer_id']}), csrf=value['csrf'])

    @bp.post('/telegram-app/api/logout')
    def logout():
        auth.logout(request.cookies.get(COOKIE, ''))
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, path='/telegram-app', secure=request.is_secure, httponly=True, samesite='None')
        return response

    customer_get = {'dashboard':'dashboard', 'products':'products', 'orders':'orders',
                    'wallet':'wallet', 'subscriptions':'subscriptions', 'referrals':'referrals',
                    'tickets':'tickets'}
    customer_post = {'quotes':'quote','orders':'create_order', 'wallet/topups':'topup',
                     'trial':'trial', 'tickets':'create_ticket'}
    # Explicit registry: request input can never select an administrator action.
    for path, action in customer_get.items():
        bp.add_url_rule('/telegram-app/api/'+path, 'customer_get_'+action,
                        lambda action=action: invoke(action), methods=['GET'])
    for path, action in customer_post.items():
        bp.add_url_rule('/telegram-app/api/'+path, 'customer_post_'+action,
                        lambda action=action: invoke(action, ticket_data() if action=='create_ticket' else data()), methods=['POST'])

    @bp.get('/telegram-app/api/orders/<int:order_id>')
    def order(order_id): return invoke('order', {'id':order_id})

    @bp.post('/telegram-app/api/orders/<int:order_id>/cancel')
    def cancel_order(order_id): return invoke('cancel_order', {'id':order_id})

    @bp.get('/telegram-app/api/subscriptions/<int:subscription_id>')
    def subscription(subscription_id): return invoke('subscription', {'id':subscription_id})

    @bp.post('/telegram-app/api/subscriptions/<int:subscription_id>/<action>-quote')
    def subscription_quote(subscription_id, action):
        if action not in {'renew','upgrade'}:
            return jsonify(error='not_found'), 404
        return invoke(action+'_quote', {**data(), 'id':subscription_id})

    @bp.route('/telegram-app/api/tickets/<int:ticket_id>/messages', methods=['GET','POST'])
    def ticket_messages(ticket_id):
        return invoke('ticket_messages' if request.method=='GET' else 'reply_ticket',
                      {**(ticket_data() if request.method=='POST' else {}),'id':ticket_id})

    @bp.get('/telegram-app/api/tickets/<int:ticket_id>')
    def ticket_detail(ticket_id): return invoke('ticket_messages',{'id':ticket_id})

    def private_attachment(value):
        reference=value.get('attachment_reference')
        if not isinstance(reference,str) or not re.fullmatch(r'[a-f0-9]{48}\.png',reference):
            return jsonify(error='not_found'),404
        file=private/'receipts'/reference
        if not file.is_file(): return jsonify(error='not_found'),404
        return send_file(file,mimetype='image/png',download_name='attachment.png')

    @bp.get('/telegram-app/api/tickets/<int:ticket_id>/attachments/<int:message_id>')
    def ticket_attachment(ticket_id,message_id):
        return private_attachment(service.customer_action(g.commerce_customer['customer_id'],
            'ticket_attachment',{'id':ticket_id,'attachment_id':message_id}))

    @bp.post('/telegram-app/api/payments/<int:payment_id>/receipt')
    def receipt(payment_id):
        # Authorize the payment before accepting a file into private storage.
        service.customer_action(g.commerce_customer['customer_id'], 'payment', {'id':payment_id})
        upload = request.files.get('file')
        identity=str(g.commerce_customer['customer_id'])+':'+request.headers['Idempotency-Key']
        reference = save_image(upload, private, identity=identity) if upload else ''
        return invoke('submit_receipt', {'payment_id':payment_id,'receipt_reference':reference,
            'transaction_text':request.form.get('transaction_text', '')})

    @bp.get('/telegram-app/api/payments/<int:payment_id>')
    def payment(payment_id): return invoke('payment', {'id':payment_id})

    @bp.get('/telegram-app/api/subscriptions/<int:subscription_id>/peers/<int:peer_id>/<kind>')
    def artifact(subscription_id, peer_id, kind):
        if kind not in {'config','qr'}:
            return jsonify(error='not_found'),404
        result = service.customer_action(g.commerce_customer['customer_id'], 'artifact',
                                         {'id':subscription_id,'peer_id':peer_id,'kind':kind})
        body = result['body']
        response = Response(body, content_type='image/png' if kind=='qr' else 'text/plain; charset=utf-8')
        response.headers['Content-Disposition'] = 'attachment; filename="wireguard-'+str(peer_id)+('.png' if kind=='qr' else '.conf')+'"'
        return response

    @bp.get('/telegram-app/api/<kind>')
    def guidance(kind):
        if kind not in {'guides','downloads','support'}:
            return jsonify(error='not_found'),404
        return jsonify(guides=[{'platform':name,'instructions':'برنامهٔ رسمی WireGuard را نصب کنید. سپس + را بزنید و فایل کانفیگ یا QR را وارد کنید. تونل را روشن کنید.',
            'url':'https://www.wireguard.com/install/'} for name in ['اندروید','iOS','ویندوز','macOS']],
            support_url='https://t.me/AshkanfathiOfficial')

    admin_get = {'dashboard':'dashboard','products':'products','payment-destinations':'destinations',
                 'payments':'payments','customers':'customers','discounts':'discounts','tickets':'tickets',
                 'jobs':'jobs','audit':'audit','announcements':'announcements'}
    admin_post = {'products':'save_product','payment-destinations':'save_destination','discounts':'save_discount'}
    for path, action in admin_get.items():
        bp.add_url_rule('/telegram-commerce/api/'+path, 'admin_get_'+action,
                        lambda action=action: invoke(action,dict(request.args),admin=True), methods=['GET'])
    for path, action in admin_post.items():
        bp.add_url_rule('/telegram-commerce/api/'+path, 'admin_post_'+action,
                        lambda action=action: invoke(action,data(),admin=True), methods=['POST'])

    @bp.put('/telegram-commerce/api/<resource>/<int:item_id>')
    def update_resource(resource, item_id):
        action = {'products':'save_product','payment-destinations':'save_destination','discounts':'save_discount'}.get(resource)
        if not action:
            return jsonify(error='not_found'),404
        return invoke(action,{**data(),'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/payment-destinations/<int:item_id>/audit')
    def destination_audit(item_id): return invoke('destination_audit',{'id':item_id},admin=True)

    @bp.post('/telegram-commerce/api/payments/<int:item_id>/review')
    def payment_review(item_id): return invoke('review_payment',{**data(),'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/payments/<int:item_id>')
    def admin_payment(item_id):return invoke('payment',{'id':item_id},admin=True)

    @bp.post('/telegram-commerce/api/announcements')
    def announce():return invoke('announce',data(),admin=True)

    @bp.post('/telegram-commerce/api/orders/<int:item_id>/refund')
    def refund(item_id):return invoke('refund_order',{**data(),'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/health')
    def health():
        value=service.admin_action(g.commerce_owner,'health',{})
        value['bot']=runtime.public()
        value['runtime']=runtime.status()
        value['csrf']=owner_csrf()
        return jsonify(value)

    @bp.get('/telegram-commerce/api/customers/<int:item_id>')
    def customer_detail(item_id): return invoke('customer',{'id':item_id},admin=True)

    @bp.put('/telegram-commerce/api/customers/<int:item_id>')
    def customer_status(item_id):return invoke('suspend_customer',{**data(),'id':item_id},admin=True)

    @bp.post('/telegram-commerce/api/customers/<int:item_id>/<operation>')
    def customer_operation(item_id,operation):
        action={'suspend':'suspend_customer','wallet-adjustment':'wallet_adjustment'}.get(operation)
        if not action:
            return jsonify(error='not_found'),404
        return invoke(action,{**data(),'id':item_id},admin=True)

    @bp.post('/telegram-commerce/api/tickets/<int:item_id>/reply')
    def admin_reply(item_id):return invoke('reply_ticket',{**ticket_data(admin=True),'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/tickets/<int:item_id>')
    def admin_ticket(item_id):return invoke('ticket_messages',{'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/tickets/<int:ticket_id>/attachments/<int:message_id>')
    def admin_attachment(ticket_id,message_id):
        return private_attachment(service.admin_action(g.commerce_owner,'ticket_attachment',
            {'id':ticket_id,'attachment_id':message_id}))

    @bp.get('/telegram-commerce/api/targets')
    def targets():
        rows=[]
        for item in adapter.targets():
            target=({'scope':'node','node_id':item['node_id'],'iface':item['iface']}
                    if item.get('scope')=='node' else {'scope':'local','iface_id':item['iface_id']})
            rows.append({**item,'target':target})
        return jsonify(targets=rows,csrf=owner_csrf())

    @bp.post('/telegram-commerce/api/jobs/<int:item_id>/reconcile')
    def reconcile(item_id):return invoke('reconcile',{'id':item_id},admin=True)

    @bp.get('/telegram-commerce/api/payments/<int:item_id>/receipt')
    def admin_receipt(item_id):
        value=service.admin_action(g.commerce_owner,'payment',{'id':item_id})
        reference=value.get('payment',value).get('receipt_reference')
        if not reference or not re.fullmatch(r'[a-f0-9]{48}\.png',reference):
            return jsonify(error='not_found'),404
        file=private/'receipts'/reference
        if not file.is_file():
            return jsonify(error='not_found'),404
        return send_file(file,mimetype='image/png',download_name='receipt.png')

    @bp.route('/telegram-commerce/api/settings', methods=['GET','PUT'])
    def settings():
        if request.method=='GET':
            value=service.admin_action(g.commerce_owner,'settings',{})
            value.setdefault('settings',{}).update(runtime.public())
            value['csrf']=owner_csrf()
            return jsonify(value)
        payload=data()
        runtime_payload={k:payload.pop(k) for k in list(payload) if k in {'bot_token','bot_enabled','public_base_url'}}
        selections={key:payload[key] for key in ('bot_target','trial_target') if key in payload}
        if selections:
            from portal_commerce_service import target_spec
            previous=service.admin_action(g.commerce_owner,'settings',{}).get('settings',{})
            changed=[]
            for key,value in selections.items():
                selected=target_spec(value,allow_empty=True)
                payload[key]=selected
                if selected and selected!=previous.get(key,{}):changed.append(selected)
            if changed:
                available=[]
                for item in adapter.targets():
                    available.append({'scope':'node','node_id':item['node_id'],'iface':item['iface']}
                        if item.get('scope')=='node' else {'scope':'local','iface_id':item['iface_id']})
                if any(selected not in available for selected in changed):
                    raise CommerceHTTPError('interface_unavailable','اینترفیس انتخاب‌شده در فهرست سرورهای در دسترس نیست؛ فهرست را تازه کنید.')
        if payload:
            service.admin_action(g.commerce_owner,'save_settings',payload,request.headers.get('Idempotency-Key'))
        if runtime_payload:
            runtime.save(runtime_payload,g.commerce_owner)
        return jsonify(settings={**service.admin_action(g.commerce_owner,'settings',{}).get('settings',{}),**runtime.public()},csrf=owner_csrf())

    if csrf is not None:
        csrf.exempt(bp)
    app.register_blueprint(bp)
    # Native WG_Panel sets DENY globally. Run this narrowly scoped response
    # policy last so the customer Mini App can be framed by Telegram only.
    def final_customer_policy(response):
        return headers(response) if request.path.startswith('/telegram-app') else response
    app.after_request_funcs.setdefault(None,[]).insert(0,final_customer_policy)
    app.extensions['portal_commerce_runtime_config']=runtime
    if not app.testing and os.name!='nt' and os.environ.get('WG_PORTAL_COMMERCE_RUNTIME','1')!='0':
        install_runtime_starter(app,service,runtime,private)


def install_runtime_starter(app,service,runtime,private):
    """Wait for WSGI serving: native application registration must finish first."""
    gate=threading.Lock()
    started_pid=None
    @app.before_request
    def start_after_setup():
        nonlocal started_pid
        pid=os.getpid()
        if started_pid==pid:return
        with gate:
            if started_pid==pid:return
            thread=threading.Thread(target=run_runtime,args=(app,service,runtime,private),
                                    daemon=True,name='telegram-commerce-supervisor')
            thread.start()
            started_pid=pid


def save_image(upload, private, *, identity=None):
    from PIL import Image, UnidentifiedImageError
    if upload is None:
        raise CommerceHTTPError('receipt_required','تصویر رسید را انتخاب کنید.')
    raw=upload.read(5*1024*1024+1)
    if not raw or len(raw)>5*1024*1024:
        raise CommerceHTTPError('invalid_receipt','حداکثر اندازهٔ تصویر رسید ۵ مگابایت است.')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in {'PNG','JPEG'} or source.width*source.height>16000000:
                raise ValueError()
            source.load()
            image=source.convert('RGB')
            output=io.BytesIO();image.save(output,format='PNG')
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
        raise CommerceHTTPError('invalid_receipt','فقط تصویر سالم PNG یا JPEG مجاز است.') from None
    directory=private/'receipts';directory.mkdir(mode=0o700,exist_ok=True)
    encoded=output.getvalue()
    reference=(hmac.new(str(current_app.secret_key).encode(),
        (identity+':'+hashlib.sha256(encoded).hexdigest()).encode(),hashlib.sha256).hexdigest()[:48]
        if identity else secrets.token_hex(24))+'.png'
    try:descriptor=os.open(directory/reference,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        if identity and (directory/reference).read_bytes()==encoded:return reference
        raise CommerceHTTPError('receipt_conflict','ثبت تصویر نیازمند بررسی است.',409) from None
    with os.fdopen(descriptor,'wb') as file:file.write(encoded)
    return reference


class RuntimeConfig:
    def __init__(self, app, store):self.app,self.store=app,store
    def cipher(self):
        from cryptography.fernet import Fernet
        import base64
        key=str(self.app.secret_key or '').encode()
        if not key:raise CommerceHTTPError('secret_required','کلید امنیتی پنل تنظیم نشده است.',503)
        return Fernet(base64.urlsafe_b64encode(hmac.new(key,b'wgportal-commerce-token-v1',hashlib.sha256).digest()))
    def row(self):
        with self.store.db(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS commerce_runtime_config (id INTEGER PRIMARY KEY CHECK(id=1), token_enc TEXT NOT NULL, enabled INTEGER NOT NULL, base_url TEXT NOT NULL, updated_at INTEGER NOT NULL)')
            if 'bot_username' not in {item[1] for item in db.execute('PRAGMA table_info(commerce_runtime_config)')}:
                db.execute("ALTER TABLE commerce_runtime_config ADD COLUMN bot_username TEXT NOT NULL DEFAULT ''")
            row=db.execute('SELECT * FROM commerce_runtime_config WHERE id=1').fetchone()
            return dict(row) if row else {'token_enc':'','enabled':0,'base_url':'','bot_username':''}
    def token(self):
        value=self.row()['token_enc']
        return self.cipher().decrypt(value.encode()).decode() if value else ''
    def ensure_isolated(self,token):
        import sys
        module=sys.modules.get(self.app.import_name)
        loader=getattr(module,'_load_tg_settings',None)
        native=(loader() or {}) if callable(loader) else {}
        candidates=[native.get('bot_token',''),os.environ.get('TG_BOT_TOKEN',''),os.environ.get('TELEGRAM_BOT_TOKEN','')]
        if any(item and str(item).split(':')[0]==token.split(':')[0] for item in candidates):
            raise CommerceHTTPError('bot_conflict','برای بات عمومی از توکن مستقل از بات مدیریت استفاده کنید.',409)
        manager=self.app.extensions.get('reseller_bots')
        if manager is not None:
            with manager.store.db() as db:
                row=db.execute('SELECT 1 FROM bots WHERE bot_id=?',(int(token.split(':')[0]),)).fetchone()
                if row:raise CommerceHTTPError('bot_conflict','این بات به یک نماینده اختصاص دارد.',409)
    def public(self):
        row=self.row()
        return {'bot_configured':bool(row['token_enc']),'bot_enabled':bool(row['enabled']),
                'public_base_url':row['base_url'],'bot_username':row.get('bot_username','')}
    def status(self,state=None):
        with self.store.db(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS commerce_runtime_status (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, heartbeat INTEGER NOT NULL)')
            if state:
                db.execute('INSERT OR REPLACE INTO commerce_runtime_status VALUES(1,?,?)',(state,int(time.time())))
            row=db.execute('SELECT state,heartbeat FROM commerce_runtime_status WHERE id=1').fetchone()
            return dict(row) if row else {'state':'not_started','heartbeat':None}
    def save(self,payload,actor):
        row=self.row()
        token=payload.get('bot_token')
        if token is not None and (not isinstance(token,str) or not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}',token)):
            raise CommerceHTTPError('invalid_bot_token','توکن بات معتبر نیست.')
        enabled=payload.get('bot_enabled',bool(row['enabled']))
        if type(enabled) is not bool:raise CommerceHTTPError('invalid_enabled','وضعیت بات معتبر نیست.')
        base=payload.get('public_base_url',row['base_url'])
        if not isinstance(base,str):raise CommerceHTTPError('invalid_url','نشانی پنل معتبر نیست.')
        url=urlsplit(base)
        if base and (url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment):
            raise CommerceHTTPError('invalid_url','نشانی عمومی باید HTTPS و بدون اطلاعات ورود باشد.')
        encrypted=self.cipher().encrypt(token.encode()).decode() if token else row['token_enc']
        username=row.get('bot_username','')
        if enabled and (not encrypted or not base):raise CommerceHTTPError('incomplete_bot','ابتدا توکن و نشانی HTTPS پنل را ثبت کنید.')
        if token or enabled:
            candidate=token or self.cipher().decrypt(encrypted.encode()).decode()
            self.ensure_isolated(candidate)
            if not self.app.testing:
                from portal_reseller_bot import TelegramTransport
                try:
                    transport=TelegramTransport()
                    info=transport.call(candidate,'getMe')
                    webhook=transport.call(candidate,'getWebhookInfo')
                except Exception:
                    raise CommerceHTTPError('bot_verification_failed','ارتباط بات تأیید نشد؛ توکن و دسترسی سرور به تلگرام را بررسی کنید.',503) from None
                if not isinstance(info,dict) or info.get('is_bot') is not True or str(info.get('id'))!=candidate.split(':')[0]:
                    raise CommerceHTTPError('invalid_bot_token','توکن بات معتبر نیست.')
                username=str(info.get('username',''))
                if not re.fullmatch(r'[A-Za-z0-9_]{5,64}',username):
                    raise CommerceHTTPError('invalid_bot_identity','هویت بات تأیید نشد.')
                if not isinstance(webhook,dict) or webhook.get('url'):
                    raise CommerceHTTPError('webhook_conflict','این بات Webhook فعال دارد؛ بات مستقل بدون Webhook معرفی کنید.',409)
        with self.store.db(write=True) as db:
            db.execute('INSERT OR REPLACE INTO commerce_runtime_config(id,token_enc,enabled,base_url,updated_at,bot_username) VALUES(1,?,?,?,?,?)',(encrypted,int(enabled),base.rstrip('/'),int(time.time()),username))
            db.execute('CREATE TABLE IF NOT EXISTS commerce_runtime_audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, created_at INTEGER NOT NULL)')
            db.execute('INSERT INTO commerce_runtime_audit(actor,action,created_at) VALUES(?,?,?)',(actor,'public_bot_configuration_changed',int(time.time())))


def run_runtime(app,service,config,private):
    import fcntl
    from portal_public_bot import PublicBot
    from portal_reseller_bot import TelegramTransport
    while True:
        try:
            with open(private/'runtime.lock','a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(5);continue
                bot=None;credential=None
                refreshed_at=0
                while True:
                    with app.app_context():
                        public=config.public()
                        if public['bot_enabled']:
                            fresh=config.token()
                            config.ensure_isolated(fresh)
                            signature=(fresh,public['public_base_url'])
                            if bot is None or credential!=signature:
                                credential=signature
                                def artifact_provider(cid,sid,pid,kind):
                                    result=service.customer_action(cid,'artifact',{'id':sid,'peer_id':pid,'kind':kind})
                                    return result['body'], 'image/png' if kind=='qr' else 'text/plain'
                                def admin_provider():
                                    key=str(app.config.get('API_KEY') or '')
                                    if not key:return []
                                    with app.app_context():
                                        response=Client(app,NativeResponse,use_cookies=False).get('/api/telegram/admins',headers={'X-API-KEY':key},base_url='https://localhost')
                                        try:
                                            value=response.get_json(silent=True) if response.status_code==200 else {}
                                        finally:response.close()
                                    return [int(row['id']) for row in value.get('admins',[]) if isinstance(row,dict) and re.fullmatch(r'[1-9][0-9]{0,19}',str(row.get('id','')))]
                                bot=PublicBot(service,TelegramTransport(),public['public_base_url'],config.token,
                                              artifact_provider=artifact_provider,admin_provider=admin_provider)
                            bot.poll_once()
                        processor=getattr(service,'process_jobs',None)
                        if callable(processor):processor()
                        if time.monotonic()-refreshed_at>=60:
                            service.refresh_subscriptions()
                            service.expire_payments()
                            refreshed_at=time.monotonic()
                        config.status('running' if public['bot_enabled'] else 'bot_disabled')
                    time.sleep(2)
        except Exception:
            # Never include HTTP URLs, credentials or receipt content in logs.
            app.logger.warning('Telegram commerce worker paused; inspect owner health/jobs panel')
            try:config.status('retrying')
            except Exception:pass
            time.sleep(15)
