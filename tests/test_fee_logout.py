"""New fee snapshots and isolated logout with temp SQLite/fake native + Telegram."""
import unittest

import test_native_reseller as native_fixture
import test_reseller_bot as bot_fixture
from deploy.linux.reseller_store import RESELLER_CREATION_FEE_TOMAN
from portal_reseller_native import ResellerSessions


class FeeLogoutChecks(unittest.TestCase):
    setUp = native_fixture.NativeResellerChecks.setUp
    post = native_fixture.NativeResellerChecks.post
    account = native_fixture.NativeResellerChecks.account
    login = native_fixture.NativeResellerChecks.login

    def test_new_owner_child_and_grandchild_use_five_million(self):
        self.assertEqual(RESELLER_CREATION_FEE_TOMAN, 5_000_000)
        self.assertEqual(self.client.get('/reseller/api/session').json['activation']['amount'], 5_000_000)
        self.client.get('/fixture-owner')
        root = self.post('/resellers/api/accounts', {'username': 'dealer', 'password': 'a-safe-test-password', 'max_peers': 3})
        self.assertEqual(root.status_code, 201, root.json)
        account = root.json['account']
        self.assertEqual(account['creation_fee_toman'], 5_000_000)
        self.assertEqual(root.json['creation_fee']['amount'], 5_000_000)
        self.assertEqual(self.post(f"/resellers/api/accounts/{account['id']}/approval", {'approved': True}).status_code, 200)
        self.login()
        metadata = self.client.get('/reseller/api/accounts').json['creation_fee']
        self.assertEqual(metadata['amount'], 5_000_000)
        self.assertIn('۵٬۰۰۰٬۰۰۰', metadata['message'])
        self.assertEqual(self.client.get('/reseller/api/session').json['activation']['amount'], 5_000_000)
        child = self.post('/reseller/api/accounts', {'username': 'child-five', 'password': 'a-safe-test-password'})
        self.assertEqual(child.status_code, 201, child.json)
        self.assertEqual(child.json['account']['creation_fee_toman'], 5_000_000)
        child_id = child.json['account']['id']
        self.assertEqual(self.post(f'/resellers/api/accounts/{child_id}/approval', {'approved': True}).status_code, 200)
        child_login = self.post('/reseller/api/login', {'username': 'child-five', 'password': 'a-safe-test-password'})
        self.csrf = child_login.json['csrf']
        grandchild = self.post('/reseller/api/accounts', {'username': 'grand-five', 'password': 'a-safe-test-password'})
        self.assertEqual(grandchild.status_code, 201, grandchild.json)
        self.assertEqual(grandchild.json['account']['creation_fee_toman'], 5_000_000)
        self.assertEqual(self.store.summary()['total_creation_fee_toman'], 15_000_000)

    def test_old_pending_fee_preserved_in_activation_and_summary(self):
        old = self.store.create('dealer', 'a-safe-test-password', [1], 3, creation_fee_toman=3_000_000)
        self.login()
        session = self.client.get('/reseller/api/session').json
        self.assertEqual(session['activation']['amount'], 3_000_000)
        self.assertEqual(session['account']['creation_fee_toman'], 3_000_000)
        self.assertEqual(self.store.get(old['id'])['creation_fee_toman'], 3_000_000)
        self.assertEqual(self.store.summary()['total_creation_fee_toman'], 3_000_000)
        self.client.get('/fixture-owner')
        approved = self.post(f"/resellers/api/accounts/{old['id']}/approval", {'approved': True})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json['account']['creation_fee_toman'], 3_000_000)

    def test_logout_clears_reseller_cookie_identity_and_preserves_native_owner_session(self):
        self.account()
        self.client.get('/fixture-owner')
        owner_cookie = self.client.get_cookie('session').value
        self.login()
        self.assertIsNotNone(self.client.get_cookie(ResellerSessions.cookie_name))
        self.assertEqual(self.client.get('/reseller/api/peers').status_code, 200)
        logged_out = self.post('/reseller/api/logout', {})
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(logged_out.json, {'ok': True, 'signin_url': '/login'})
        self.assertIsNone(self.client.get_cookie(ResellerSessions.cookie_name))
        self.assertEqual(self.client.get('/reseller/api/peers').status_code, 401)
        self.assertIsNone(self.client.get('/reseller/api/session').json['account'])
        self.assertEqual(self.client.get('/reseller').status_code, 302)
        self.assertEqual(self.client.get_cookie('session').value, owner_cookie)
        self.assertEqual(self.client.get('/api/admin').status_code, 200)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 200)


class BotFeeChecks(unittest.TestCase):
    setUp = bot_fixture.BotChecks.setUp
    post = bot_fixture.BotChecks.post
    account = bot_fixture.BotChecks.account
    login = bot_fixture.BotChecks.login
    configure = bot_fixture.BotChecks.configure
    update = bot_fixture.BotChecks.update

    def test_child_wizard_uses_recorded_five_million_fee(self):
        row, _ = self.configure()
        self.manager.command(row, self.update('👥 ساخت نماینده', update_id=810))
        self.manager.command(row, self.update('bot-child-five', update_id=811))
        answer = self.manager.command(row, self.update('a-safe-test-password', update_id=812))
        self.assertEqual(answer[0], 'messages')
        self.assertIn('۵٬۰۰۰٬۰۰۰', answer[1][0]['text'])
        self.assertNotIn('۳٬۰۰۰٬۰۰۰', answer[1][0]['text'])
        children = self.store.children(self.parent['id'])
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['creation_fee_toman'], 5_000_000)


if __name__ == '__main__':
    unittest.main()
