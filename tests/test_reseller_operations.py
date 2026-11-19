import importlib.util, os, sys, tempfile, unittest
from pathlib import Path
from flask import Flask
ROOT=Path(__file__).resolve().parents[1]
SUPPORT=Path(os.environ['OPERATIONS_SUPPORT_ROOT'])
for p in (SUPPORT,SUPPORT/'deploy/linux',ROOT,ROOT/'deploy/linux'): sys.path.insert(0,str(p))
spec=importlib.util.spec_from_file_location('operations_routes',ROOT/'deploy/linux/portal_reseller.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
class Operations(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.store=r.ResellerStore(Path(self.tmp.name)/'db');self.owner='mother';outer=self
  class Core:
   def json(self,method,path,payload=None,*,cookie=None):
    if path=='/api/admin': return (200,{'username':outer.owner}) if outer.owner and cookie=='session=native' else (401,{})
    raise AssertionError(path)
  self.app=Flask(__name__);self.app.config.update(TESTING=True,SECRET_KEY='test',SESSION_COOKIE_NAME='reseller-test')
  self.app.register_blueprint(r.reseller_blueprint('http://unused',self.store.path,'unused',core=Core()))
  self.client=self.app.test_client();self.client.set_cookie('session','native')
  self.account=self.store.create('resellerone','long-enough-password',[1],20)
  self.csrf=self.client.get('/reseller/api/session').json['csrf']
 def send(self,method,path,value=None,key='temporary-notice-key-123'):
  return self.client.open(path,method=method,json=value,headers={'Origin':'https://localhost','X-Reseller-CSRF':self.csrf,'Idempotency-Key':key})
 def enter(self): return self.send('POST',f"/resellers/api/accounts/{self.account['id']}/enter")
 def test_entry_return_pending_and_cookie(self):
  self.assertEqual(self.enter().json,{'url':'/reseller'})
  state=self.client.get('/reseller/api/session').json
  self.assertEqual(state['admin_context']['owner_username'],'mother');self.assertFalse(state['account']['active'])
  self.assertEqual(self.client.get('/reseller/api/peers').status_code,403)
  self.assertEqual(self.send('POST','/reseller/api/return-owner').json,{'url':'/resellers'})
  self.assertEqual(self.client.get_cookie('session').value,'native')
  self.assertIsNone(self.client.get('/reseller/api/session').json['account'])
 def test_revalidation(self):
  self.enter();self.owner='changed'
  self.assertIsNone(self.client.get('/reseller/api/session').json['account'])
  self.assertEqual(self.client.get('/reseller/api/credit').status_code,401)
  self.assertEqual(self.client.get('/reseller/api/notices').status_code,401)
  self.assertEqual(self.send('POST','/reseller/api/return-owner').status_code,403)
  self.owner=None;self.assertEqual(self.enter().status_code,401)
 def test_deleted_and_csrf(self):
  self.assertEqual(self.client.post(f"/resellers/api/accounts/{self.account['id']}/enter").status_code,403)
  self.store.archive(self.account['id']);self.assertEqual(self.enter().status_code,404)
 def test_notices(self):
  value={'title':'خبر <script>','body':'پیام ساده\nهمه'}
  first=self.send('POST','/resellers/api/notices',value);self.assertEqual(first.status_code,201,first.json)
  self.assertEqual(self.send('POST','/resellers/api/notices',value).json,first.json)
  self.assertEqual(self.send('POST','/resellers/api/notices',dict(value,body='different')).status_code,400)
  self.enter();public=self.client.get('/reseller/api/notices').json['notices']
  self.assertEqual(public[0]['body'],value['body']);self.assertNotIn('actor',public[0])
  nid=public[0]['id'];self.assertEqual(self.send('PUT',f'/resellers/api/notices/{nid}',{'active':False}).status_code,200)
  self.assertEqual(self.client.get('/reseller/api/notices').json['notices'],[])
  with self.store.db() as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
  self.assertEqual(len([x for x in self.store.audit() if x['action'].startswith('notice:create')]),1)
 def test_child_and_no_owner_power(self):
  self.send('POST','/resellers/api/notices',{'title':'خبر','body':'همه'})
  self.store.approve(self.account['id'])
  child=self.store.create('childone','long-enough-password',[],10,parent_id=self.account['id'],allow_unassigned=True)
  with self.client.session_transaction() as s: s['reseller_identity']={'id':child['id'],'version':child['auth_version']}
  self.client.delete_cookie('session')
  self.assertEqual(len(self.client.get('/reseller/api/notices').json['notices']),1)
  self.assertEqual(self.send('POST','/resellers/api/notices',{'title':'fake','body':'fake'}).status_code,401)
 def test_invalid_inputs(self):
  for value in ({'title':'','body':'x'},{'title':'x','body':'x\x00'},{'title':'x','body':'x','actor':'forged'}):
   self.assertEqual(self.send('POST','/resellers/api/notices',value).status_code,400)
  self.assertEqual(self.send('POST','/resellers/api/notices',{'title':'x','body':'x'},key='short').status_code,400)
if __name__=='__main__': unittest.main()
