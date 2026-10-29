"""Native administrator bot's reseller workspace. Native admin_only remains the gate.

No bot credentials or native administrator sessions are constructed here. Every
operation goes through the narrowly authenticated native administration bridge.
"""
import asyncio
from html import escape
import secrets
import time

STATE = 'reseller_admin_flow'
FIELDS = {
    'endpoint_host': ('میزبان اتصال بدون پورت', str), 'dns': ('DNS', str),
    'mtu': ('MTU', int), 'allowed_ips': ('مسیرهای مجاز', str),
    'persistent_keepalive': ('فاصلهٔ Keepalive', int), 'network_pool': ('محدودهٔ شبکه', str),
    'subnet_prefix': ('اندازهٔ زیرشبکه', int), 'child_max_peers': ('سقف پیش‌فرض Peer', int),
    'auto_firewall': ('فایروال خودکار؛ ۱ فعال / ۰ غیرفعال', bool)}


def kb(rows):
    return {'inline_keyboard': [[{'text': title, 'callback_data': data} for title, data in row] for row in rows]}


def back(target='resellers:menu'):
    return kb([[('↩️ بازگشت', target)]])


def digits(value):
    return str(value).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))


def error_text(result):
    messages = {
        'interface_review_required': 'ساخت اینترفیس نیازمند بررسی است؛ از بازیابی اینترفیس استفاده کنید و ساخت را تکرار نکنید.',
        'provision_defaults_required': 'ابتدا پیش‌فرض اینترفیس را در تنظیمات نمایندگی ذخیره کنید.',
        'interface_create_uncertain': 'حساب ثبت شد ولی نتیجهٔ ساخت اینترفیس نامشخص است؛ حساب را دوباره نسازید.',
        'admin_required': 'دسترسی مدیر تأیید نشد.', 'forbidden': 'دسترسی مدیر تأیید نشد.',
        'rate_limited': 'تعداد درخواست زیاد است؛ کمی بعد تلاش کنید.'}
    return messages.get(result.get('error'), 'عملیات تأیید نشد؛ وضعیت حساب را بررسی کنید. اطلاعات محرمانه را در پیام خطا نفرستید.')


async def call(update, api_call, action, payload=None, request_id=None):
    user = update.effective_user
    body = {'action': action, 'payload': payload or {},
        'actor': {'id': str(user.id), 'username': str(getattr(user, 'username', '') or '')},
        'request_id': request_id or secrets.token_hex(16)}
    result = await asyncio.to_thread(api_call, 'POST', '/api/portal/reseller-admin', payload=body, timeout=90)
    if not isinstance(result, dict):
        return {'error': 'invalid_admin_response'}
    if result.get('error') or result.get('ok') is False:
        return result
    field = 'accounts' if action == 'list' else 'defaults' if action in {'defaults_get','defaults_set'} else 'account'
    expected = list if field == 'accounts' else dict
    if not isinstance(result.get(field), expected):
        return {'error': 'invalid_admin_response'}
    if field == 'account' and type(result[field].get('id')) is not int:
        return {'error': 'invalid_admin_response'}
    return result


async def listing(update, context, api_call, send, page=0, parent=None):
    data = await call(update, api_call, 'list')
    if data.get('error') or data.get('ok') is False:
        await send(update, error_text(data), back()); return
    accounts = data.get('accounts', [])
    if parent is not None:
        accounts = [a for a in accounts if a.get('parent_id') == parent]
    page = max(0, min(page, max(0, (len(accounts)-1)//8)))
    lines = ['👥 <b>مدیریت نماینده‌ها</b>', f'تعداد: {len(accounts)}', 'هزینهٔ ثبت حساب جدید: ۵٬۰۰۰٬۰۰۰ تومان', 'حساب موردنظر را انتخاب کنید.']
    rows = []
    for account in accounts[page*8:page*8+8]:
        marker = '⏳' if account.get('approval_status') != 'approved' else '✅' if account.get('active') else '⏸'
        rows.append([(marker+' '+str(account.get('username',''))[:40], f"resellers:open:{account['id']}")])
    nav = []
    if page: nav.append(('⬅️ قبلی',f'resellers:page:{page-1}'))
    if (page+1)*8 < len(accounts): nav.append(('بعدی ➡️',f'resellers:page:{page+1}'))
    if nav: rows.append(nav)
    rows += [[('➕ ساخت نماینده','resellers:create'),('⚙️ پیش‌فرض اینترفیس','resellers:defaults')],
             [('🔄 تازه‌سازی','resellers:menu'),('🏠 منوی اصلی','home:main')]]
    await send(update, '\n'.join(lines), kb(rows))


async def detail(update, api_call, send, account_id):
    data = await call(update, api_call, 'detail', {'id': account_id})
    if data.get('error') or data.get('ok') is False:
        await send(update, error_text(data), back()); return
    account = data.get('account') or {}
    if not account:
        await send(update, 'نماینده پیدا نشد.', back()); return
    parent = account.get('parent_username') or 'مدیر اصلی'
    state = 'حساب بسته‌شده' if account.get('deleted') else 'در انتظار تأیید پرداخت' if account.get('approval_status') != 'approved' else 'فعال' if account.get('active') else 'متوقف'
    lines = ['👤 <b>'+escape(str(account.get('username','')) )+'</b>',
        f'شناسه: {account_id}', 'والد: '+escape(str(parent)), 'وضعیت: '+state,
        'سقف Peer: '+str(account.get('max_peers',0)),
        'ورود دومرحله‌ای: '+('فعال' if account.get('twofa_enabled') else 'غیرفعال'),
        'زیرمجموعهٔ مستقیم / کل: '+str(account.get('direct_child_count',0))+' / '+str(account.get('descendant_count',0)),
        'هزینهٔ ثبت این حساب: '+format(int(account.get('creation_fee_toman',0)), ',')+' تومان',
        'اینترفیس: '+escape(', '.join(str(i) for i in account.get('interfaces',[])) or 'در انتظار بررسی')]
    rows = []
    if not account.get('deleted'):
        approval = 'revoke' if account.get('approval_status') == 'approved' else 'approve'
        toggle = 'disable' if account.get('enabled') else 'enable'
        rows += [[('💳 '+('لغو تأیید پرداخت' if approval=='revoke' else 'تأیید پرداخت'),f'resellers:{approval}:{account_id}')],
                 [('⏸ توقف' if toggle=='disable' else '▶️ فعال‌کردن',f'resellers:{toggle}:{account_id}'),('📅 تمدید',f'resellers:renew:{account_id}')],
                 [('📊 سقف Peer',f'resellers:quota:{account_id}'),('🛠 بازیابی اینترفیس',f'resellers:setinterface:{account_id}')],
                 [('🔐 بازنشانی ورود دومرحله‌ای',f'resellers:reset2fa:{account_id}')],
                 [('🗃 بستن حساب با حفظ مشتری‌ها',f'resellers:archive:{account_id}')]]
    rows += [[('👥 زیرمجموعه‌ها',f'resellers:children:{account_id}')], [('🔄 تازه‌سازی',f'resellers:open:{account_id}'),('↩️ فهرست','resellers:menu')]]
    await send(update, '\n'.join(lines), kb(rows))


def flow(context, action, account_id=None, fields=None, payload=None):
    state = {'action':action,'id':account_id,'fields':fields or [],'values':payload or {},
             'expires':time.monotonic()+300,'request_id':secrets.token_hex(16)}
    context.user_data[STATE] = state
    return state


async def confirm(update, context, send, action, account_id, payload=None):
    state = flow(context,action,account_id,payload=payload)
    descriptions = {'approve':'تأیید دریافت هزینه و فعال‌سازی اولیه', 'revoke':'لغو تأیید پرداخت و توقف دسترسی',
        'enable':'فعال‌کردن دسترسی؛ تأیید پرداخت و اعتبار همچنان بررسی می‌شوند', 'disable':'توقف دسترسی حساب و زیرمجموعه‌ها',
        'archive':'بستن حساب و بات با حفظ Peerها، زیرمجموعه‌ها و سابقه', 'reset2fa':'بازنشانی ورود دومرحله‌ای این حساب',
        'renew':'افزودن '+str(state['values'].get('days',''))+' روز به اعتبار', 'quota':'تغییر سقف Peer', 'setinterface':'ثبت اینترفیس بررسی‌شده برای بازیابی',
        'defaults_set':'ذخیرهٔ پیش‌فرض ساخت اینترفیس نمایندگان جدید'}
    await send(update, '⚠️ <b>تأیید عملیات</b>\n'+escape(descriptions.get(action, action))+'\nشناسه: '+str(account_id or '—'),
        kb([[('✅ تأیید', 'resellers:confirm:'+state['request_id'])],[('❌ لغو','resellers:menu')]]))


async def handle_callback(update, context, data, api_call, send):
    if not str(data).startswith('resellers:'):
        context.user_data.pop(STATE,None); return False
    parts = str(data).split(':')
    action = parts[1] if len(parts)>1 else ''
    if action=='confirm':
        state=context.user_data.get(STATE)
        if not state or state['expires']<time.monotonic() or len(parts)!=3 or parts[2]!=state['request_id']:
            context.user_data.pop(STATE,None); await send(update,'درخواست منقضی شده است؛ دوباره از فهرست انتخاب کنید.',back()); return True
        payload={**state['values']}
        if state['id'] is not None: payload['id']=state['id']
        if state['action'] in {'archive','reset2fa'}: payload['confirm']=True
        result=await call(update,api_call,state['action'],payload,state['request_id'])
        if result.get('error') or result.get('ok') is False:
            await send(update,error_text(result),kb([[('🔄 بررسی مجدد با همان شناسه','resellers:confirm:'+state['request_id'])],[('↩️ فهرست','resellers:menu')]])); return True
        context.user_data.pop(STATE,None)
        if result.get('warning'): await send(update,'دسترسی حساب بسته شد؛ پاک‌سازی بات نیازمند بررسی است.',back())
        else: await send(update,'✅ عملیات ثبت شد.',back())
        return True
    context.user_data.pop(STATE,None)
    if action in {'menu','page'}:
        page=int(parts[2]) if action=='page' and len(parts)==3 and parts[2].isdigit() else 0
        await listing(update,context,api_call,send,page); return True
    if action=='create':
        flow(context,'create',fields=['username','password','max_peers']); await send(update,'نام کاربری نماینده را بفرستید.',back()); return True
    if action=='defaults':
        result=await call(update,api_call,'defaults_get')
        if result.get('error') or result.get('ok') is False: await send(update,error_text(result),back()); return True
        defaults=result.get('defaults') or {}
        context.user_data['reseller_admin_defaults']=defaults
        rows=[[(label,f'resellers:field:{field}')] for field,(label,kind) in FIELDS.items()]
        rows.append([('↩️ فهرست','resellers:menu')])
        text='⚙️ <b>پیش‌فرض ساخت اینترفیس</b>\n'+'\n'.join(escape(label)+': <code>'+escape(str(defaults.get(field,'—')))+'</code>' for field,(label,kind) in FIELDS.items())
        await send(update,text,kb(rows)); return True
    if action=='field' and len(parts)==3 and parts[2] in FIELDS:
        field=parts[2]; values=context.user_data.get('reseller_admin_defaults')
        if not isinstance(values,dict): await send(update,'ابتدا صفحهٔ پیش‌فرض‌ها را باز کنید.',back()); return True
        state=flow(context,'defaults_set',fields=[field],payload=dict(values)); state['field']=field
        await send(update,'مقدار جدید «'+escape(FIELDS[field][0])+'» را بفرستید.',back('resellers:defaults')); return True
    if len(parts)!=3 or not parts[2].isdigit() or not 0<int(parts[2])<10**12:
        await send(update,'گزینه معتبر نیست.',back()); return True
    account_id=int(parts[2])
    if action=='open': await detail(update,api_call,send,account_id)
    elif action=='children': await listing(update,context,api_call,send,parent=account_id)
    elif action in {'renew','quota','setinterface'}:
        field={'renew':'days','quota':'max_peers','setinterface':'iface_id'}[action]
        flow(context,action,account_id,[field]); await send(update,{'days':'تعداد روز تمدید (۱ تا ۳۶۶) را بفرستید.','max_peers':'سقف Peer (۱ تا ۱۰۰۰۰) را بفرستید.','iface_id':'شناسهٔ اینترفیس واقعی بررسی‌شده را بفرستید؛ این گزینه برای بازیابی است.'}[field],back(f'resellers:open:{account_id}'))
    elif action in {'approve','revoke','enable','disable','archive','reset2fa'}: await confirm(update,context,send,action,account_id)
    else: await send(update,'گزینه معتبر نیست.',back())
    return True


async def handle_text(update, context, api_call, send):
    state=context.user_data.get(STATE)
    if not state: return False
    if state['expires']<time.monotonic():
        context.user_data.pop(STATE,None); await send(update,'درخواست منقضی شد؛ دوباره شروع کنید.',back()); return True
    value=str(update.effective_message.text or '').strip()
    if value in {'/cancel','-'}:
        context.user_data.pop(STATE,None); await send(update,'لغو شد.',back()); return True
    if not state['fields']:
        await send(update,'از دکمهٔ تأیید یا لغو استفاده کنید.',back()); return True
    field=state['fields'][0]
    if field in {'username','password'}:
        if field == 'password': value = str(update.effective_message.text or '')
        if not value or len(value)>(1024 if field=='password' else 64) or (field=='password' and len(value)<12):
            await send(update,'مقدار معتبر بفرستید؛ رمز باید حداقل ۱۲ نویسه باشد.',back()); return True
        parsed=value
    elif state['action']=='defaults_set':
        kind=FIELDS[field][1]
        try:
            parsed=bool(int(digits(value))) if kind is bool and digits(value) in {'0','1'} else int(digits(value)) if kind is int else value if kind is str else None
            if parsed is None: raise ValueError()
        except ValueError:
            await send(update,'مقدار معتبر بفرستید.',back('resellers:defaults')); return True
    else:
        try: parsed=int(digits(value))
        except ValueError: parsed=0
        maximum=366 if field=='days' else 10000 if field=='max_peers' else 10**12-1
        if not 1<=parsed<=maximum:
            await send(update,'عدد معتبر در محدودهٔ مجاز بفرستید.',back()); return True
    state['values'][field]=parsed; state['fields'].pop(0)
    if state['fields']:
        prompt='رمز اولیه (حداقل ۱۲ نویسه) را بفرستید؛ آن را در این گفتگو بازنشر نمی‌کنم.' if state['fields'][0]=='password' else 'سقف Peer را بفرستید (۱ تا ۱۰۰۰۰).'
        await send(update,prompt,back()); return True
    if state['action']=='create':
        try:
            result=await call(update,api_call,'create',dict(state['values']),state['request_id'])
        finally:
            context.user_data.pop(STATE,None); state['values'].pop('password',None)
        if result.get('error') or result.get('ok') is False:
            await send(update,error_text(result),back()); return True
        account=result.get('account') or {}
        await send(update,'✅ حساب و اینترفیس خودکار ساخته شدند.\nنام کاربری: '+escape(str(account.get('username','')))+'\nبرای فعال‌سازی، تأیید پرداخت جداگانه لازم است.',back()); return True
    payload=dict(state['values'])
    if state['action']=='defaults_set': payload=payload
    await confirm(update,context,send,state['action'],state['id'],payload)
    return True
