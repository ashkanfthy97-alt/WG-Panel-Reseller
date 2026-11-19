import json,sys,tempfile,unittest
from pathlib import Path
from flask import Flask,jsonify,request,Response
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portal_commerce_store import Store
from portal_commerce_native import NativeAdapter,NativeError

class NativeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'commerce.sqlite3')
        self.app=Flask(__name__);self.app.config.update(TESTING=True,API_KEY='test-native-secret')
        self.created=[];self.peers=[];self.effects=0;self.lose_response=False;outer=self
        @self.app.before_request
        def gate():
            self.assertEqual(request.headers.get('X-API-KEY'),'test-native-secret')
            self.assertIsNone(request.headers.get('Cookie'))
        @self.app.get('/api/subscriptions/locations')
        def targets():return jsonify(local=[{'scope':'local','iface_id':1,'iface':'wg0','available':20}],nodes=[{'id':2,'online':True,'interfaces':[{'iface':'wg1','available':10}]}])
        @self.app.route('/api/subscriptions',methods=['GET','POST'])
        def subscriptions():
            if request.method=='GET':return jsonify(subscriptions=self.created)
            payload=request.json;self.effects+=1
            sid=len(self.created)+1;locations=[]
            for i,target in enumerate(payload['targets']):
                pid=len(self.peers)+1;self.peers.append({'id':pid,'public_key':'public'+str(pid)+'='})
                locations.append({'peer_id':pid,'public_key':'public'+str(pid)+'=','link_id':pid,'name':'device'+str(i)})
            row={'id':sid,'token':'native-sensitive-token-'+str(sid),'note':payload['note'],'locations':locations,'used_bytes':4,'remaining_bytes':1024,'ttl_seconds':86400,'access':{'allowed':True},**payload}
            self.created.append(row)
            if self.lose_response:raise RuntimeError('response lost after committed effect')
            return jsonify(subscription=row),201
        @self.app.route('/api/subscriptions/<int:sid>',methods=['GET','PUT'])
        def sub(sid):
            if request.method=='PUT':self.created[sid-1].update(request.json)
            return jsonify(subscription=self.created[sid-1])
        @self.app.get('/api/peers')
        def peers():return jsonify(peers=self.peers)
        @self.app.post('/api/subscriptions/<int:sid>/disable')
        def disable(sid):
            self.created[sid-1]['enabled']=False;self.created[sid-1]['access']={'allowed':False,'reason':'disabled'}
            return jsonify(ok=True)
        @self.app.get('/api/peer/<path:public>/config')
        def config(public):
            if not any(p['public_key']==public for p in self.peers):return jsonify(error='not_found'),404
            return Response('[Interface]\nPrivateKey=sensitivefixture\n',content_type='text/plain')
        @self.app.get('/api/peer/<path:public>/config_qr')
        def qr(public):return Response(b'\x89PNGtest',content_type='image/png')
        self.adapter=NativeAdapter(self.app,self.store)
        self.spec={'customer_id':4,'product_name':'آزمایش','traffic_gb':10,'days':30,'devices':2,'target':{'scope':'local','iface_id':1}}
    def test_native_multidevice_replay_and_sensitive_artifact(self):
        result=self.adapter.provision('order-operation-123',self.spec)
        self.assertEqual(len(result['peers']),2);self.assertEqual(self.effects,1)
        self.assertNotIn('private',str(result).lower());self.assertNotIn('token',result)
        self.adapter.provision('order-operation-123',self.spec);self.assertEqual(self.effects,1)
        body,mime=self.adapter.artifact(result['id'],result['peers'][0]['id'],'config')
        self.assertIn(b'PrivateKey=',body)
        with self.assertRaises(NativeError):self.adapter.artifact(result['id'],999,'config')
    def test_ambiguous_create_reconciles_without_duplicate(self):
        self.lose_response=True
        result=self.adapter.provision('ambiguous-operation-123',self.spec)
        self.assertEqual(result['id'],1);self.assertEqual(self.effects,1)
        self.adapter.provision('ambiguous-operation-123',self.spec);self.assertEqual(self.effects,1)
    def test_unlimited_traffic_keeps_expiry_and_target_capacity(self):
        spec=dict(self.spec,traffic_gb=None)
        self.adapter.provision('unlimited-operation-123',spec)
        self.assertEqual(self.created[0]['data_limit_value'],0)
        self.assertFalse(self.created[0]['unlimited']);self.assertEqual(self.created[0]['time_limit_days'],30)
        with self.assertRaises(NativeError):self.adapter.provision('bad-target-operation-123',dict(spec,target={'scope':'local','iface_id':999}))
        self.assertEqual(self.effects,1)
    def test_recycled_key_and_subscription_token_fail_closed_even_replay(self):
        self.adapter.provision('immutable-operation-123',self.spec)
        self.peers[0]['public_key']='recycled'
        for call in [lambda:self.adapter.subscription(1),lambda:self.adapter.artifact(1,1,'qr'),lambda:self.adapter.provision('immutable-operation-123',self.spec)]:
            with self.assertRaises(NativeError):call()
        self.assertEqual(self.effects,1)
    def test_renewal_uses_native_put_and_carries_quota_time(self):
        self.adapter.provision('first-operation-123',self.spec)
        result=self.adapter.provision('renew-operation-123',dict(self.spec,action='renew',existing_native_id=1))
        self.assertEqual(result['id'],1);self.assertEqual(self.effects,1)
        self.assertEqual(self.created[0]['time_limit_days'],31)
        self.assertEqual(self.created[0]['data_limit_value'],20)
        self.assertTrue(self.created[0]['reset_timer'])
    def test_fractional_trial_duration_and_idempotent_refund_suspend(self):
        self.adapter.provision('trial-operation-123',dict(self.spec,days=0.5))
        self.assertEqual(self.created[0]['time_limit_days'],0.5)
        first=self.adapter.suspend('refund-operation-123',{'existing_native_id':1})
        second=self.adapter.suspend('refund-operation-123',{'existing_native_id':1})
        self.assertEqual(first['status'],'suspended');self.assertEqual(first,second)
        with self.assertRaises(NativeError):self.adapter.artifact(1,1,'config')
    def test_renewing_unlimited_to_finite_keeps_new_quota_usable(self):
        self.adapter.provision('unlimited-first-123',dict(self.spec,traffic_gb=None))
        self.created[0]['used_bytes']=500*1024**3
        self.adapter.provision('finite-renewal-123',dict(self.spec,action='renew',existing_native_id=1))
        self.assertEqual(self.created[0]['data_limit_value'],510)
    def test_recycling_between_validation_and_download_uses_old_key_only(self):
        self.adapter.provision('race-safe-operation-123',self.spec)
        original=self.adapter._subscription_row
        def race(native_id):
            row=original(native_id)
            self.peers[0]['public_key']='new-owner-key'
            return row
        self.adapter._subscription_row=race
        with self.assertRaises(NativeError):self.adapter.artifact(1,1,'config')

if __name__=='__main__':unittest.main()
