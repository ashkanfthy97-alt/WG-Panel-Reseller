"""Mock native login and Linux socket files; never bind or create an Interface."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from flask import Flask, request, redirect, session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portal_provisioning import system_ports, occupied_ports, suggest_port, child_network, provisioning_lock, ProvisionError
from portal_reseller_native import install_reseller, reseller_username_taken
from deploy.linux.reseller_store import ResellerStore


class PortsChecks(unittest.TestCase):
    def test_all_protocols_configs_and_reservations_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for offset, protocol in enumerate(('tcp','tcp6','udp','udp6')):
                (root/protocol).write_text('header\n 0: 00000000:%04X 00000000:0000 07\n' % (51825+offset))
            (root/'inactive.conf').write_text('[Interface]\nListenPort = 51829 # stopped\n')
            ports = occupied_ports([{'listen_port':51830}], root,
                [{'port':51831,'state':'uncertain'}], scanner=lambda:system_ports(root))
            self.assertEqual(suggest_port(ports),51832)
            (root/'udp6').unlink()
            with self.assertRaises(ProvisionError):
                system_ports(root)

    def test_network_does_not_overlap_and_lock_is_reentrant(self):
        self.assertEqual(child_network([{'address':'10.0.0.1/24'}]), '10.0.1.1/24')
        self.assertEqual(child_network([{'address':'10.66.66.1/24',
            'scope_networks':['10.0.0.0/24']}]), '10.0.1.1/24')
        with tempfile.TemporaryDirectory() as directory:
            with provisioning_lock(directory):
                with provisioning_lock(directory):
                    self.assertTrue(Path(directory,'interface-provision.lock').is_file())


class LoginChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'resellers.sqlite3'
        self.app=Flask(__name__,root_path=str(ROOT),instance_path=self.temp.name)
        self.app.config.update(TESTING=True,SECRET_KEY='unified-login-test-'*4)
        self.native=[]
        @self.app.route('/login',methods=['GET','POST'])
        def login():
            self.native.append(request.form.to_dict())
            if request.form.get('twofa_code')!='123456':
                return 'native 2FA required',401
            session['_user_id']='mother'
            return redirect('/')
        self.csrf=Mock()
        with patch.dict(os.environ,{'WG_PANEL_RESELLER_DB':str(self.path)}):
            install_reseller(self.app,self.csrf,native_admin_lookup=lambda name:name.casefold()=='mother')
        self.store=ResellerStore(self.path)
        self.app.config['RESELLER_NATIVE_CSRF_FACTORY'] = lambda: 'native-fixture-csrf'
        self.client=self.app.test_client()

    def test_pending_reseller_uses_common_login_with_independent_cookie(self):
        account=self.store.create('dealer','long-test-password',[1])
        result=self.client.post('/login',data={'username':'dealer','password':'long-test-password'})
        self.assertEqual(result.status_code,302)
        self.assertEqual(result.location,'/reseller')
        self.assertFalse(self.native)
        self.csrf.protect.assert_called_once()
        value=self.client.get('/reseller/api/session').json['account']
        self.assertEqual(value['id'],account['id'])
        self.assertFalse(value['active'])
        with self.client.session_transaction() as native_session:
            self.assertNotIn('_user_id',native_session)

    def test_native_username_never_falls_back_after_2fa_failure(self):
        self.store.create('mother','long-test-password',[1])
        result=self.client.post('/login',data={'username':'mother','password':'long-test-password'})
        self.assertEqual(result.status_code,401)
        self.assertEqual(len(self.native),1)
        self.assertIsNone(self.client.get('/reseller/api/session').json['account'])

    def test_native_success_clears_previous_reseller_cookie(self):
        self.store.create('dealer','long-test-password',[1])
        self.client.post('/login',data={'username':'dealer','password':'long-test-password'})
        self.assertIsNotNone(self.client.get('/reseller/api/session').json['account'])
        result=self.client.post('/login',data={'username':'mother','twofa_code':'123456'})
        self.assertEqual(result.status_code,302)
        self.assertIsNone(self.client.get('/reseller/api/session').json['account'])

    def test_native_account_name_guard_is_case_insensitive(self):
        self.store.create('dealer','long-test-password',[1])
        self.assertTrue(reseller_username_taken(self.app,'DEALER'))
        self.assertFalse(reseller_username_taken(self.app,'other'))


if __name__=='__main__':
    unittest.main()
