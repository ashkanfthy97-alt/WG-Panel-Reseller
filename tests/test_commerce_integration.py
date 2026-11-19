"""Real domain/auth/HTTP, with an isolated native adapter and temporary files."""
import hashlib,hmac,io,json,sys,tempfile,time,unittest
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from PIL import Image
from portal_commerce import install_commerce
from portal_commerce_auth import Auth
from portal_commerce_service import CommerceService
from portal_commerce_store import Store

TOKEN='123456:abcdefghijklmnopqrstuvwxyz_fixture_123'
class Native:
    def __init__(self,*args):self.calls=0;self.created={}
    def provision(self,operation,spec):
        self.calls+=1;row={'id':100+self.calls,'status':'active','used_bytes':0,'remaining_bytes':1000,'expires_at':int(time.time())+86400,'peers':[{'id':90+self.calls,'name':'fixture','public_key':'fixture-public-key'}]};self.created[operation]=row;return row
    def reconcile(self,operation):return self.created.get(operation)
    def subscription(self,native_id):return next(row for row in self.created.values() if row['id']==native_id)
    def artifact(self,native_id,peer_id,kind):return b'fixture-private-config','text/plain'
    def targets(self):return [{'scope':'local','iface_id':1,'available':100},{'scope':'local','iface_id':2,'available':100}]

class Integrated(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.owner=True;self.app=Flask(__name__,instance_path=self.tmp.name)
        self.app.config.update(TESTING=True,SECRET_KEY='temporary-secret',COMMERCE_STORE_FACTORY=Store,COMMERCE_SERVICE_FACTORY=CommerceService,COMMERCE_AUTH_FACTORY=Auth,COMMERCE_NATIVE_FACTORY=Native,COMMERCE_OWNER_CHECK=lambda:'mother' if self.owner else None)
        install_commerce(self.app,None);self.service=self.app.extensions['portal_commerce_service'];self.client=self.app.test_client();self.admin=self.app.test_client()
        self.app.extensions['portal_commerce_runtime_config'].save({'bot_token':TOKEN,'public_base_url':'https://localhost','bot_enabled':False},'fixture-owner')
        fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':1201,'first_name':'مشتری'})}
        secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest();fields['hash']=hmac.new(secret,'\n'.join(k+'='+fields[k] for k in sorted(fields)).encode(),hashlib.sha256).hexdigest()
        self.init_data=urlencode(fields)
        response=self.client.post('/telegram-app/api/auth',json={'init_data':self.init_data},base_url='https://localhost',headers={'Origin':'https://localhost'})
        self.assertEqual(response.status_code,200,response.json);self.csrf=response.json['csrf'];self.cid=response.json['customer']['id']
        self.admin_csrf=self.admin.get('/telegram-commerce/api/settings',base_url='https://localhost').json['csrf']
    def send(self,client,method,path,value=None,key='integration-key-123456',csrf=None):
        return client.open(path,method=method,json=value,base_url='https://localhost',headers={'Origin':'https://localhost','X-Commerce-CSRF':csrf or self.csrf,'Idempotency-Key':key})
    def test_purchase_review_and_protected_delivery(self):
        destination={'card_number':'1111222233334444','cardholder_name':'شخص آزمایشی','bank_name':'','active':True}
        result=self.send(self.admin,'POST','/telegram-commerce/api/payment-destinations',destination,csrf=self.admin_csrf)
        self.assertEqual(result.status_code,200,result.json)
        product=self.send(self.admin,'POST','/telegram-commerce/api/products',{'name':'محصول آزمایشی','category':'economy','traffic_gb':10,'days':30,'devices':1,'price_toman':100000,'active':True,'target':{'scope':'local','iface_id':1}},csrf=self.admin_csrf).json['product']
        quote=self.send(self.client,'POST','/telegram-app/api/quotes',{'product_id':product['id']}).json['quote']
        order=self.send(self.client,'POST','/telegram-app/api/orders',{'quote_id':quote['id'],'payment_method':'card'},key='integration-order-12345').json['order'];pid=order['payment_id']
        image=io.BytesIO();Image.new('RGB',(10,10),'white').save(image,format='PNG');raw=image.getvalue()
        def upload():return self.client.post('/telegram-app/api/payments/'+str(pid)+'/receipt',base_url='https://localhost',data={'file':(io.BytesIO(raw),'receipt.png'),'transaction_text':'رسید آزمایشی'},headers={'Origin':'https://localhost','X-Commerce-CSRF':self.csrf,'Idempotency-Key':'receipt-integration-12345'})
        first=upload();self.assertEqual(first.status_code,200,first.json);self.assertEqual(upload().json,first.json)
        self.assertEqual(self.service.native.calls,0)
        for key in ['review-integration-first','review-integration-again']:
            result=self.send(self.admin,'POST',f'/telegram-commerce/api/payments/{pid}/review',{'decision':'approved'},key=key,csrf=self.admin_csrf);self.assertEqual(result.status_code,200,result.json)
        self.service.process_jobs();self.service.process_jobs();self.assertEqual(self.service.native.calls,1)
        sub=self.client.get('/telegram-app/api/subscriptions',base_url='https://localhost').json['subscriptions'][0]
        result=self.client.get(f"/telegram-app/api/subscriptions/{sub['id']}/peers/{sub['peers'][0]['id']}/config",base_url='https://localhost');self.assertEqual(result.status_code,200,result.json);self.assertIn(b'fixture-private-config',result.data)
        self.owner=False;self.assertEqual(self.client.get(f'/telegram-commerce/api/payments/{pid}/receipt',base_url='https://localhost').status_code,401)
        with self.app.extensions['portal_commerce_store'].db() as db:
            encrypted=db.execute('SELECT token_enc FROM commerce_runtime_config').fetchone()[0];self.assertNotIn(TOKEN,encrypted)
    def test_replay_suspension_and_file_validation(self):
        repeated=self.client.post('/telegram-app/api/auth',json={'init_data':self.init_data},base_url='https://localhost',headers={'Origin':'https://localhost'})
        self.assertNotEqual(repeated.status_code,200)
        self.service.admin_action('mother','suspend_customer',{'id':self.cid,'suspended':True})
        response=self.client.get('/telegram-app/api/dashboard',base_url='https://localhost');self.assertIn(response.status_code,{401,403})
    def test_settings_reject_unknown_interface_before_saving_any_selection(self):
        value={'bot_target':{'scope':'local','iface_id':999},'trial_target':{'scope':'local','iface_id':2}}
        result=self.send(self.admin,'PUT','/telegram-commerce/api/settings',value,csrf=self.admin_csrf)
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(result.json['error'],'interface_unavailable')
        settings=self.service.admin_action('mother','settings')['settings']
        self.assertEqual(settings.get('bot_target',{}),{})
        self.assertEqual(settings['trial_target'],{})
    def test_two_interface_settings_drive_independent_paid_and_trial_jobs(self):
        selected={'bot_target':{'scope':'local','iface_id':2},'trial_target':{'scope':'local','iface_id':1},'trial_enabled':True}
        result=self.send(self.admin,'PUT','/telegram-commerce/api/settings',selected,csrf=self.admin_csrf)
        self.assertEqual(result.status_code,200,result.json)
        product=self.send(self.admin,'POST','/telegram-commerce/api/products',{'name':'مقصد پیش‌فرض بات','category':'economy','traffic_gb':10,'days':30,'devices':1,'price_toman':100000,'active':True,'target':{}},csrf=self.admin_csrf).json['product']
        quote=self.send(self.client,'POST','/telegram-app/api/quotes',{'product_id':product['id']}).json['quote']
        self.service.admin_action('mother','wallet_adjustment',{'id':self.cid,'amount_toman':100000,'reason':'اعتبار آزمایشی'},'integration-interface-funds')
        result=self.send(self.client,'POST','/telegram-app/api/orders',{'quote_id':quote['id'],'payment_method':'wallet'},key='integration-interface-order')
        self.assertEqual(result.status_code,200,result.json)
        trial=self.send(self.client,'POST','/telegram-app/api/trial',{},key='integration-interface-trial')
        self.assertEqual(trial.status_code,200,trial.json)
        with self.app.extensions['portal_commerce_store'].db() as db:
            jobs=[json.loads(row['spec']) for row in db.execute('SELECT spec FROM provisioning_jobs ORDER BY id')]
        self.assertEqual(jobs[0]['target'],selected['bot_target'])
        self.assertEqual(jobs[1]['target'],selected['trial_target'])

if __name__=='__main__':unittest.main()
