"""Narrow commerce adapter to native WG_Panel subscription provisioning.

Native subscriptions own allocation, node installation, compensation and limits.
The durable intent journal never repeats an ambiguous create. Administrator
changes that replace a native id/key fail closed at credential delivery.
"""
import hashlib
import json
import math
import re
import time
from urllib.parse import quote
from werkzeug.test import Client
from werkzeug.wrappers import Response


class NativeError(ValueError):
    def __init__(self,code='native_unavailable'):
        self.code=code;self.status=502;self.message='ارتباط با سرویس WireGuard کامل نشد؛ وضعیت سفارش را بررسی کنید و ساخت را تکرار نکنید.'
        super().__init__(code)


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS commerce_native_operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, native_id INTEGER, tag TEXT NOT NULL, bindings TEXT, created_at INTEGER NOT NULL)')


class NativeAdapter:
    def __init__(self,app,store=None,*,clock=None):
        self.app=app;self.store=store or app.extensions.get('portal_commerce_store');self.clock=clock or time.time

    def _call(self,method,path,payload=None):
        allowed=(method=='GET' and path in {'/api/subscriptions','/api/subscriptions/locations','/api/peers'}
                 or method=='GET' and re.fullmatch(r'/api/subscriptions/[1-9][0-9]*',path)
                 or method=='GET' and re.fullmatch(r'/api/peer/[A-Za-z0-9%_+\-=]+/(?:config\?download=1|config_qr)',path)
                 or method=='POST' and path=='/api/subscriptions'
                 or method=='PUT' and re.fullmatch(r'/api/subscriptions/[1-9][0-9]*',path)
                 or method=='POST' and re.fullmatch(r'/api/subscriptions/[1-9][0-9]*/disable',path))
        if not allowed:raise NativeError('unsupported_native_operation')
        key=str(self.app.config.get('API_KEY') or '')
        if not key:raise NativeError('native_credential_missing')
        # No request context, browser cookie or Flask-Login g is inherited.
        with self.app.app_context():
            response=Client(self.app,Response,use_cookies=False).open(path,method=method,json=payload,
                headers={'X-API-KEY':key,'Accept':'application/json'},base_url='https://localhost',buffered=False)
            try:
                body=bytearray()
                for chunk in response.iter_encoded():
                    body.extend(chunk)
                    if len(body)>16*1024*1024:raise NativeError('native_response_too_large')
                return response.status_code,bytes(body),response.headers.get('Content-Type','')
            finally:response.close()

    def _json(self,method,path,payload=None):
        status,body,_=self._call(method,path,payload)
        try:value=json.loads(body)
        except (ValueError,UnicodeError):raise NativeError() from None
        if status not in {200,201} or not isinstance(value,dict) or value.get('error'):raise NativeError()
        return value

    def targets(self):
        value=self._json('GET','/api/subscriptions/locations')
        if not isinstance(value.get('local'),list) or not isinstance(value.get('nodes'),list):raise NativeError('native_inventory_invalid')
        result=[]
        for item in value['local']:
            if isinstance(item,dict):result.append(dict(item,scope='local'))
        for node in value['nodes']:
            if not isinstance(node,dict) or not node.get('online'):continue
            for item in node.get('interfaces',[]):
                if isinstance(item,dict):result.append(dict(item,scope='node',node_id=node['id']))
        return result

    @staticmethod
    def _target_key(target):
        if not isinstance(target,dict):raise NativeError('target_required')
        if target.get('scope','local')=='local':return ('local',target.get('iface_id'))
        return ('node',target.get('node_id'),target.get('iface'))

    def _target(self,spec):
        key=self._target_key(spec.get('target'))
        found=next((target for target in self.targets() if self._target_key(target)==key),None)
        devices=spec.get('devices',1)
        if type(devices) is not int or not 1<=devices<=5:raise NativeError('invalid_devices')
        if found is None or int(found.get('available',0))<devices:raise NativeError('server_capacity_unavailable')
        allowed=('scope','iface_id','iface','node_id','listen_port','server_cidr','interface_address','label','location_label','country_code','flag')
        return {key:found[key] for key in allowed if key in found}

    def _record(self,operation_id):
        if self.store is None:raise NativeError('native_journal_required')
        with self.store.db(write=True) as db:
            _ensure(db)
            row=db.execute('SELECT * FROM commerce_native_operations WHERE operation_id=?',(operation_id,)).fetchone()
        return dict(row) if row else None

    def _inventory(self):
        result=self._json('GET','/api/peers')
        if not isinstance(result.get('peers'),list):raise NativeError('native_inventory_invalid')
        return {peer['id']:peer for peer in result['peers'] if isinstance(peer,dict) and type(peer.get('id')) is int}

    def _bind(self,operation_id,row):
        locations=row.get('locations')
        if not isinstance(locations,list) or not locations:raise NativeError('native_subscription_incomplete')
        peers=self._inventory();bindings=[]
        for location in locations:
            peer=peers.get(location.get('peer_id'))
            key=location.get('public_key')
            if not isinstance(key,str) or not key or (peer or {}).get('public_key')!=key:raise NativeError('native_inventory_invalid')
            bindings.append({'id':peer['id'],'public_key':key,'link_id':location.get('link_id')})
        token=row.get('token')
        if not isinstance(token,str) or not token:raise NativeError('native_subscription_incomplete')
        data={'token_hash':hashlib.sha256(token.encode()).hexdigest(),'peers':bindings}
        with self.store.db(write=True) as db:
            _ensure(db)
            db.execute("UPDATE commerce_native_operations SET state='complete',native_id=?,bindings=? WHERE operation_id=?",(row['id'],json.dumps(data),operation_id))
        return self._public(row,peers)

    def _public(self,row,inventory=None):
        if inventory is None:
            bindings=self._bindings(row['id'])
            inventory={peer['id']:peer for peer in bindings['peers']}
        access=row.get('access') or {}
        status='active' if access.get('allowed',row.get('enabled',False)) else 'expired' if access.get('reason') in {'time_expired','expired'} else 'suspended'
        return {'id':row['id'],'status':status,'name':row.get('name',''),'used_bytes':row.get('used_bytes'),
            'limit_bytes':row.get('limit_bytes'),'remaining_bytes':row.get('remaining_bytes'),
            'expires_at':row.get('expires_at'),'expires_at_ts':row.get('expires_at_ts'),'remaining_seconds':row.get('ttl_seconds'),
            'peers':[{'id':p.get('peer_id'),'public_key':inventory.get(p.get('peer_id'),{}).get('public_key'),'name':p.get('name',''),'location_label':p.get('location_label',''),'used_bytes':p.get('used_bytes'),
                'connected':p.get('connected'),'runtime_available':p.get('runtime_available')} for p in row.get('locations',[])]}

    def reconcile(self,operation_id):
        operation=self._record(operation_id)
        if not operation:return None
        if operation['state']=='complete':return self.subscription(operation['native_id'])
        value=self._json('GET','/api/subscriptions')
        if not isinstance(value.get('subscriptions'),list):raise NativeError('native_inventory_invalid')
        rows=[row for row in value['subscriptions'] if isinstance(row,dict) and operation['tag'] in str(row.get('note','')).splitlines()]
        if len(rows)>1:raise NativeError('duplicate_native_operation_requires_review')
        if rows:
            if operation.get('native_id'):
                self._subscription_row(operation['native_id'])
            return self._bind(operation_id,rows[0])
        if operation['state']=='complete':raise NativeError('native_subscription_missing')
        return None

    def provision(self,operation_id,spec):
        if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,100}',operation_id):raise NativeError('invalid_operation_id')
        if not isinstance(spec,dict):raise NativeError('invalid_specification')
        fingerprint=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        existing=self._record(operation_id)
        if existing:
            if existing['fingerprint']!=fingerprint:raise NativeError('operation_conflict')
            result=self.reconcile(operation_id)
            if result:return result
            raise NativeError('provisioning_uncertain_requires_review')
        days=spec.get('days')
        traffic=spec.get('traffic_gb')
        if type(days) not in {int,float} or not math.isfinite(days) or not 1/24<=days<=3660 or traffic is not None and (type(traffic) is not int or not 1<=traffic<=1000000):raise NativeError('invalid_specification')
        action=spec.get('action','create')
        tag='wgportal-commerce:'+operation_id
        payload={'name':str(spec.get('product_name') or 'اشتراک WireGuard')[:80],
            'note':tag,'data_limit_value':traffic or 0,'data_limit_unit':'Gi','time_limit_days':days,
            'start_on_first_use':False,'unlimited':False,'telegram_id':str(spec.get('customer_id',''))}
        native_id=spec.get('existing_native_id')
        if action in {'renew','upgrade'}:
            original=self._subscription_row(native_id)
            # Keep original ownership tag. PUT resets the native timer; carry
            # remaining duration explicitly, and preserve cumulative usage.
            note=str(original.get('note','')).splitlines()[0]
            payload['note']=note+'\n'+tag
            if action=='renew':
                payload['time_limit_days']=days+max(0,int(original.get('ttl_seconds') or 0))/86400
                if traffic:
                    used_gib=math.ceil(max(0,int(original.get('used_bytes') or 0))/1024**3)
                    payload['data_limit_value']=max(int(original.get('data_limit_value') or 0),used_gib)+traffic
            elif traffic:
                payload['data_limit_value']=math.ceil(max(0,int(original.get('used_bytes') or 0))/1024**3)+traffic
            payload.pop('name');payload.pop('telegram_id')
            payload['reset_timer']=True
            path='/api/subscriptions/'+str(native_id);method='PUT'
        elif action=='create':
            target=self._target(spec)
            payload['targets']=[dict(target) for _ in range(spec.get('devices',1))]
            path='/api/subscriptions';method='POST'
        else:raise NativeError('invalid_native_action')
        # The intent commit precedes native effects. A worker crash or response
        # loss can only enter reconcile/manual review, never blind recreation.
        try:
            with self.store.db(write=True) as db:
                _ensure(db)
                db.execute('INSERT INTO commerce_native_operations VALUES(?,?,?,?,?,?,?)',(operation_id,fingerprint,'provisioning',native_id,tag,None,int(self.clock())))
        except Exception:
            if self._record(operation_id):raise NativeError('provisioning_in_progress') from None
            raise
        try:
            result=self._json(method,path,payload)
            row=result.get('subscription')
            if not isinstance(row,dict):raise NativeError()
            return self._bind(operation_id,row)
        except Exception:
            try:
                recovered=self.reconcile(operation_id)
                if recovered:return recovered
            except Exception:pass
            raise NativeError('provisioning_uncertain_requires_review') from None

    def _subscription_row(self,native_id):
        if type(native_id) is not int or native_id<1:raise NativeError('subscription_unavailable')
        binding=self._bindings(native_id)
        row=self._json('GET','/api/subscriptions/'+str(native_id)).get('subscription')
        if not isinstance(row,dict) or row.get('id')!=native_id:raise NativeError('subscription_unavailable')
        token=str(row.get('token',''))
        if not hashlib.sha256(token.encode()).hexdigest()==binding['token_hash']:raise NativeError('native_identity_changed')
        current={p.get('peer_id'):p for p in row.get('locations',[])}
        inventory=self._inventory()
        for peer in binding['peers']:
            if peer['id'] not in current or current[peer['id']].get('public_key')!=peer['public_key'] or (inventory.get(peer['id']) or {}).get('public_key')!=peer['public_key']:
                raise NativeError('native_identity_changed')
        if set(current)!=set(p['id'] for p in binding['peers']):raise NativeError('native_identity_changed')
        return row

    def _bindings(self,native_id):
        with self.store.db(write=True) as db:
            _ensure(db)
            operations=db.execute("SELECT * FROM commerce_native_operations WHERE native_id=? AND state='complete' ORDER BY created_at DESC",(native_id,)).fetchall()
        if not operations:raise NativeError('subscription_unavailable')
        return json.loads(operations[0]['bindings'])

    def subscription(self,native_id):return self._public(self._subscription_row(native_id))

    def artifact(self,native_id,peer_id,kind):
        if kind not in {'config','qr'}:raise NativeError('invalid_artifact')
        row=self._subscription_row(native_id)
        if not (row.get('access') or {}).get('allowed',row.get('enabled',False)):raise NativeError('subscription_suspended')
        if type(peer_id) is not int or peer_id not in {p.get('peer_id') for p in row.get('locations',[])}:raise NativeError('peer_not_owned')
        peer=next((p for p in self._bindings(native_id)['peers'] if p['id']==peer_id),None)
        if peer is None:raise NativeError('peer_not_owned')
        path='/api/peer/'+quote(peer['public_key'],safe='')+('/config?download=1' if kind=='config' else '/config_qr')
        status,body,content_type=self._call('GET',path)
        if status!=200 or not body or kind=='qr' and not body.startswith(b'\x89PNG') or kind=='config' and (b'[Interface]' not in body or b'PrivateKey' not in body):raise NativeError('artifact_unavailable')
        return body,'image/png' if kind=='qr' else 'text/plain; charset=utf-8'

    def renew(self,operation_id,spec):return self.provision(operation_id,dict(spec,action='renew'))
    def upgrade(self,operation_id,spec):return self.provision(operation_id,dict(spec,action='upgrade'))
    def suspend(self,operation_id,spec=None):
        native_id=operation_id if spec is None else spec.get('existing_native_id')
        self._subscription_row(native_id)
        fingerprint=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest() if spec is not None else None
        if spec is not None:
            if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,100}',operation_id):raise NativeError('invalid_operation_id')
            operation=self._record(operation_id)
            if operation and operation['fingerprint']!=fingerprint:raise NativeError('operation_conflict')
            with self.store.db(write=True) as db:
                _ensure(db)
                db.execute('INSERT OR IGNORE INTO commerce_native_operations VALUES(?,?,?,?,?,?,?)',(operation_id,fingerprint,'provisioning',native_id,'suspend:'+operation_id,None,int(self.clock())))
        self._json('POST','/api/subscriptions/'+str(native_id)+'/disable',{})
        result=self.subscription(native_id)
        if result['status']=='active':raise NativeError('native_suspension_unconfirmed')
        if spec is not None:
            with self.store.db(write=True) as db:
                original=db.execute("SELECT bindings FROM commerce_native_operations WHERE native_id=? AND state='complete' AND bindings IS NOT NULL LIMIT 1",(native_id,)).fetchone()
                db.execute("UPDATE commerce_native_operations SET state='complete',bindings=? WHERE operation_id=?",(original['bindings'],operation_id))
        return result
