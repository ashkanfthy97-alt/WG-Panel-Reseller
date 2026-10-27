"""Child Interface admission with an entirely simulated native create boundary."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_native_reseller as fixture
from portal_reseller_native import NativeApplicationCore


class ChildInterfaceChecks(unittest.TestCase):
    post=fixture.NativeResellerChecks.post
    account=fixture.NativeResellerChecks.account
    login=fixture.NativeResellerChecks.login

    def setUp(self):
        fixture.NativeResellerChecks.setUp(self)
        self.parent=self.account()
        self.login()
        result=self.post('/reseller/api/accounts',dict(username='child',password='long-test-password',max_peers=2))
        self.assertEqual(result.status_code,201)
        self.child=result.json['account']
        self.assertEqual(self.child['interfaces'],[])
        self.created=[]
        self.native_result=201, {'interface':{'id':3,'name':'wg_r'+str(self.child['id']),'address':'10.0.0.1/24','listen_port':23457}}
        adapter=NativeApplicationCore(self.app)
        def native_json(method,path,payload=None,**kwargs):
            if path=='/api/portal/provision-child-interface':
                self.created.append(payload)
                return self.native_result
            return adapter.json(method,path,payload,**kwargs)
        self.app.config['RESELLER_CORE']=SimpleNamespace(json=native_json,call=adapter.call)

    def test_port_suggestion_reject_collision_and_dedicated_assignment(self):
        path=f"/reseller/api/accounts/{self.child['id']}"
        with patch('portal_provisioning.occupied_ports',return_value={20000,23456}):
            self.assertEqual(self.client.get(path+'/interface-port').json['port'],20001)
            self.assertEqual(self.post(path+'/interface',{'port':23456}).status_code,409)
            self.assertFalse(self.created)
            response=self.post(path+'/interface',{'port':23457})
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        self.assertEqual(self.store.get(self.child['id'])['interfaces'],[3])
        self.assertEqual(self.created[0]['endpoint_host'],'vpn.example.test')
        self.assertEqual(self.created[0]['listen_port'],23457)
        self.assertNotIn('private_key',self.created[0])
        self.assertFalse(self.store.get(self.child['id'])['active'])
        self.assertEqual(self.post(path+'/interface',{'port':23458}).status_code,400)

    def test_uncertain_native_outcome_blocks_retry_and_activation(self):
        path=f"/reseller/api/accounts/{self.child['id']}/interface"
        self.native_result=201,{'interface':{'id':3},'up_error':'simulated failure'}
        with patch('portal_provisioning.occupied_ports',return_value=set()):
            self.assertEqual(self.post(path,{'port':23457}).status_code,502)
        child=self.store.get(self.child['id'])
        self.assertEqual(child['interface_provision']['state'],'uncertain')
        self.assertEqual(child['interfaces'],[])
        self.assertEqual(self.post(path,{'port':23458}).status_code,400)
        self.assertEqual(len(self.created),1)

    def test_parent_cannot_provision_foreign_child_or_override_network_settings(self):
        other=self.store.create('other','long-test-password',[2])
        self.store.approve(other['id'])
        foreign=self.store.create('foreign','long-test-password',[],parent_id=other['id'],allow_unassigned=True)
        self.assertEqual(self.client.get(f"/reseller/api/accounts/{foreign['id']}/interface-port").status_code,404)
        self.assertEqual(self.post(f"/reseller/api/accounts/{self.child['id']}/interface",{'port':23457,'address':'0.0.0.0/0'}).status_code,400)
        self.assertFalse(self.created)


if __name__=='__main__':
    unittest.main()
