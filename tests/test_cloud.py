import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from urllib.parse import parse_qs, urlsplit

import app as core

HAS_CLOUD = all(importlib.util.find_spec(name) for name in ('flask', 'cryptography', 'pywebpush'))
if HAS_CLOUD:
    import cloud
    from cloud_identity import digest, Identity
    from cloud_push import validate_subscription, NoRedirectSession
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

ORIGIN = 'https://workdesk.example.com'
TOKEN = 'fictional-oauth-token-only-used-in-tests'


@unittest.skipUnless(HAS_CLOUD, 'Install requirements-cloud.txt to test hosted mode')
class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            'WORKDESK_DATA_DIR': self.temp.name, 'WORKDESK_PUBLIC_URL': ORIGIN,
            'WORKDESK_OWNER_ID': '42', 'WORKDESK_SECRET_KEY': 'fictional-long-encryption-secret-for-tests-only',
            'GITHUB_CLIENT_ID': 'test-client', 'GITHUB_CLIENT_SECRET': 'test-secret',
            'WORKDESK_SYNC_INTERVAL': '1800',
        })
        env.start(); self.addCleanup(env.stop)
        provider = patch.object(core, 'HOSTED_TOKEN_PROVIDER', None)
        provider.start(); self.addCleanup(provider.stop)
        state = patch.dict(core.SYNC_STATE, {'running': False})
        state.start(); self.addCleanup(state.stop)
        self.web = cloud.create_app()
        self.web.testing = True
        self.client = self.web.test_client()
        self.identity = self.web.extensions['workdesk_identity']
        self.push = self.web.extensions['workdesk_push']

    def get(self, path, client=None, **kwargs):
        return (client or self.client).get(path, base_url=ORIGIN, buffered=True, **kwargs)

    def login(self, client=None, user_id=42):
        client = client or self.client
        begin = self.get('/auth/login', client)
        state = parse_qs(urlsplit(begin.location).query)['state'][0]
        with patch('cloud_identity.github_json', side_effect=[{'access_token': TOKEN}, {'id': user_id, 'login': 'test-user'}]):
            return self.get('/auth/callback?state=' + state + '&code=fake-code', client)

    def csrf(self, client=None):
        return self.get('/api/dashboard', client).json['csrf_token']

    def post(self, path, data, client=None, **kwargs):
        client = client or self.client
        return client.post(path, json=data, base_url=ORIGIN,
                           headers={'Origin': ORIGIN, 'X-Workdesk-CSRF': self.csrf(client)}, **kwargs)

    def subscription(self, suffix='one'):
        key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {'endpoint': 'https://web.push.apple.com/Q' + suffix,
                'keys': {'auth': base64.urlsafe_b64encode(b'x'*16).decode().rstrip('='),
                         'p256dh': base64.urlsafe_b64encode(key).decode().rstrip('=')}}

    def add_notification(self, seq=1):
        with core.db_conn() as con:
            con.execute('''INSERT OR REPLACE INTO notifications VALUES
                ('test-user',?,'Private repository title','private/repo','Issue','mention',
                 'https://github.com/private/repo/issues/1',?,'revision',1,?)''',
                (str(seq), core.utcnow(), seq))

    def export_fixture(self):
        item = core.save_manual({'title': 'Saved task', 'notes': 'Keep my private note', 'priority': 1})
        with core.db_conn() as con:
            payload = {table: [dict(r) for r in con.execute(f'SELECT * FROM {table}')]
                       for table in (*cloud.IMPORT_TABLES, 'settings')}
            for table in cloud.IMPORT_TABLES:
                con.execute(f'DELETE FROM {table}')
        return payload, item

    def test_all_work_and_export_routes_require_login(self):
        for path in ('/api/dashboard', '/api/notifications', '/api/export', '/api/export.csv', '/api/backup', '/api/not-real'):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('csrf_token', response.json)
        self.assertEqual(self.get('/').location, '/login')
        self.assertEqual(self.get('/api/health').json, {'ok': True})

    def test_owner_login_and_security_headers(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/')
        cookie = response.headers.getlist('Set-Cookie')[0]
        for flag in ('Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/'):
            self.assertIn(flag, cookie)
        data = self.get('/api/dashboard')
        self.assertTrue(data.json['hosted'])
        self.assertEqual(data.json['cloud']['login'], 'test-user')
        self.assertEqual(data.headers['Cache-Control'], 'no-store')
        self.assertIn("script-src 'self'", data.headers['Content-Security-Policy'])
        self.assertNotIn(TOKEN, data.text)

    def test_other_github_account_is_rejected(self):
        self.assertEqual(self.login(user_id=43).status_code, 403)
        self.assertIsNone(self.identity.get('github_token'))
        self.assertEqual(self.get('/api/dashboard').status_code, 401)

    def test_state_mismatch_and_replay_cannot_exchange_code(self):
        state, _ = self.identity.begin()
        with patch('cloud_identity.github_json') as network:
            with self.assertRaises(ValueError):
                self.identity.finish(state, 'wrong-cookie', 'code')
            network.assert_not_called()
        with patch('cloud_identity.github_json', side_effect=[{'access_token': TOKEN}, {'id': 42, 'login': 'test-user'}]):
            self.identity.finish(state, state, 'code')
        with patch('cloud_identity.github_json') as network:
            with self.assertRaises(ValueError):
                self.identity.finish(state, state, 'code')
            network.assert_not_called()

    def test_pkce_and_expired_flow(self):
        state, url = self.identity.begin()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['redirect_uri'], [ORIGIN + '/auth/callback'])
        self.assertEqual(len(query['code_challenge'][0]), 43)
        with self.identity.db() as con:
            con.execute('UPDATE oauth SET expires=0')
        with self.assertRaises(ValueError):
            self.identity.finish(state, state, 'code')

    def test_csrf_and_origin_are_required(self):
        self.login()
        for headers in ({}, {'Origin': ORIGIN}, {'Origin': 'https://other.example', 'X-Workdesk-CSRF': self.csrf()}):
            response = self.client.post('/api/tasks', json={'title': 'Bad'}, base_url=ORIGIN, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post('/api/tasks', {'title': 'Good'}).status_code, 201)

    def test_host_header_and_forwarded_host_are_not_trusted(self):
        response = self.client.get('/login', base_url='https://attacker.example', headers={'X-Forwarded-Host': 'workdesk.example.com'})
        self.assertEqual(response.status_code, 400)

    def test_sessions_expire_and_logout_revokes(self):
        self.login()
        self.assertEqual(self.post('/auth/logout', {}).status_code, 200)
        self.assertEqual(self.get('/api/dashboard').status_code, 401)
        self.login()
        with self.identity.db() as con:
            con.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.get('/api/dashboard').status_code, 401)

    def test_two_devices_share_task_edits(self):
        self.login()
        second = self.web.test_client()
        self.login(second)
        item = self.post('/api/tasks', {'title': 'Both devices', 'notes': 'Original'}).json
        response = second.patch('/api/tasks/' + item['id'], json={'notes': 'Edited on phone'}, base_url=ORIGIN,
                                headers={'Origin': ORIGIN, 'X-Workdesk-CSRF': self.csrf(second)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get('/api/dashboard').json['tasks'][0]['notes'], 'Edited on phone')
        self.post('/auth/logout', {})
        self.assertEqual(self.get('/api/dashboard', second).status_code, 200)

    def test_credentials_are_encrypted_and_excluded_from_backups(self):
        self.login()
        self.assertEqual(self.identity.token(), TOKEN)
        self.assertNotIn(TOKEN.encode(), self.identity.path.read_bytes())
        for route in ('/api/export', '/api/backup', '/api/export.csv'):
            data = self.get(route).data
            self.assertNotIn(TOKEN.encode(), data)
            self.assertNotIn(b'cloud-identity', data)
        backup = Path(self.temp.name) / 'downloaded-backup.sqlite3'
        backup.write_bytes(self.get('/api/backup').data)
        con = sqlite3.connect(backup)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {'sessions', 'oauth', 'subscriptions', 'metadata'})
        con.close()

    def test_identity_survives_restart_and_owner_is_bound(self):
        self.login()
        again = Identity(Path(self.temp.name), self.identity.config)
        self.assertEqual(again.token(), TOKEN)
        with self.assertRaisesRegex(ValueError, 'different GitHub account'):
            Identity(Path(self.temp.name), {**self.identity.config, 'OWNER_ID': '43'})
        with self.assertRaisesRegex(ValueError, 'SECRET_KEY'):
            Identity(Path(self.temp.name), {**self.identity.config, 'SECRET_KEY': 'another-key'*5})

    def test_expiring_oauth_tokens_refresh(self):
        self.identity.store_token({'access_token': 'expired', 'refresh_token': 'refresh', 'expires_at': 0})
        with patch('cloud_identity.github_json', return_value={'access_token': 'new-token', 'refresh_token': 'new-refresh', 'expires_in': 3600}) as network:
            self.assertEqual(self.identity.token(), 'new-token')
            self.assertEqual(self.identity.token(), 'new-token')
            network.assert_called_once()
            self.assertEqual(network.call_args.args[1]['grant_type'], 'refresh_token')

    def test_account_and_local_operations_cannot_be_changed(self):
        self.login()
        self.assertEqual(self.post('/api/settings', {'github_username': 'someone-else'}).status_code, 400)
        for path in ('/api/reminder', '/api/notification-watcher'):
            self.assertEqual(self.post(path, {'action': 'install'}).status_code, 404)

    def test_import_preserves_saved_notes_and_refuses_overwrite(self):
        self.login()
        payload, item = self.export_fixture()
        self.assertEqual(self.post('/api/import', payload).status_code, 200)
        saved = self.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(saved['id'], item['id'])
        self.assertEqual(saved['notes'], 'Keep my private note')
        self.assertEqual(saved['priority'], 1)
        self.assertEqual(self.post('/api/import', payload).status_code, 400)

    def test_bad_import_rolls_back_all_rows(self):
        self.login()
        payload, _ = self.export_fixture()
        payload['settings'].append({'key': 'contribution_repos', 'value': '["not a repo"]'})
        self.assertEqual(self.post('/api/import', payload).status_code, 400)
        self.assertEqual(self.get('/api/dashboard').json['tasks'], [])

    def test_foreign_account_import_rejected(self):
        self.login()
        payload, _ = self.export_fixture()
        payload['settings'].append({'key': 'last_synced_user', 'value': '"someone-else"'})
        self.assertEqual(self.post('/api/import', payload).status_code, 400)

    def test_invalid_import_data_does_not_break_dashboard(self):
        self.login()
        payload, _ = self.export_fixture()
        payload['tasks'][0]['labels'] = '{}'
        self.assertEqual(self.post('/api/import', payload).status_code, 400)
        self.assertEqual(self.get('/api/dashboard').status_code, 200)

    def test_public_install_assets_and_no_api_cache(self):
        for path in ('/login', '/sw.js', '/manifest.webmanifest', '/static/icon-192.png', '/static/offline.html'):
            self.assertEqual(self.get(path).status_code, 200, path)
        manifest = self.get('/manifest.webmanifest').json
        self.assertEqual(manifest['display'], 'standalone')
        worker = self.get('/sw.js').text
        self.assertNotIn('cache.put', worker)
        self.assertIn("cache.add('/static/offline.html')", worker)

    def test_push_baseline_and_delivery_are_deduplicated(self):
        self.login()
        self.add_notification(1)
        self.assertEqual(self.post('/api/push/subscribe', self.subscription()).status_code, 200)
        with patch('cloud_push.webpush', return_value=Mock(status_code=201)) as send:
            self.push.flush(); send.assert_not_called()
            self.add_notification(2)
            self.push.flush(); self.push.flush()
            send.assert_called_once()
            body = send.call_args.kwargs['data']
            self.assertNotIn('Private repository', body)
            self.assertIn('1 new GitHub update', body)

    def test_push_retries_without_losing_checkpoint(self):
        self.login()
        self.post('/api/push/subscribe', self.subscription())
        self.add_notification()
        with patch('cloud_push.webpush', side_effect=RuntimeError('private endpoint')) as send:
            self.push.flush(); self.push.flush()
            send.assert_called_once()
        with self.identity.db() as con:
            device = con.execute('SELECT * FROM subscriptions').fetchone()
            self.assertEqual(device['last_seq'], 0)
            self.assertNotIn('private endpoint', device['error'])
            con.execute('UPDATE subscriptions SET retry_at=0')
        with patch('cloud_push.webpush', return_value=Mock(status_code=201)) as send:
            self.push.flush(); send.assert_called_once()

    def test_expired_push_endpoint_is_removed(self):
        self.login()
        self.post('/api/push/subscribe', self.subscription())
        self.add_notification()
        error = RuntimeError('expired')
        error.status_code = 410
        with patch('cloud_push.webpush', side_effect=error):
            self.push.flush()
        self.assertEqual(self.push.status()['devices'], 0)

    def test_disabling_alerts_skips_delivery_and_old_backlog(self):
        self.login()
        self.post('/api/push/subscribe', self.subscription())
        self.add_notification()
        self.post('/api/settings', {'desktop_notifications': False})
        with patch('cloud_push.webpush') as send:
            self.push.flush()
            self.post('/api/settings', {'desktop_notifications': True})
            self.push.flush()
            send.assert_not_called()

    def test_logout_removes_only_that_devices_subscriptions(self):
        self.login()
        second = self.web.test_client(); self.login(second)
        self.post('/api/push/subscribe', self.subscription('one'))
        self.post('/api/push/subscribe', self.subscription('two'), second)
        self.assertEqual(self.push.status()['devices'], 2)
        self.post('/auth/logout', {})
        self.assertEqual(self.push.status()['devices'], 1)

    def test_push_endpoints_reject_ssrf(self):
        payload = self.subscription()
        for endpoint in ('http://web.push.apple.com/x', 'https://127.0.0.1/x', 'https://169.254.169.254/latest',
                         'https://web.push.apple.com.evil.example/x', 'https://evil.example@web.push.apple.com/x',
                         'https://web.push.apple.com:8443/x'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_subscription({**payload, 'endpoint': endpoint})
        self.assertEqual(validate_subscription(payload), payload)
        with patch('requests.Session.request', return_value=Mock()) as send:
            NoRedirectSession().post(payload['endpoint'])
            self.assertFalse(send.call_args.kwargs['allow_redirects'])

    def test_real_push_encryption_without_network(self):
        self.login()
        self.post('/api/push/subscribe', self.subscription())
        self.add_notification()
        import requests
        response = requests.Response(); response.status_code = 201; response._content = b''
        with patch('cloud_push.NoRedirectSession.post', return_value=response) as send:
            self.push.flush()
            send.assert_called_once()
            self.assertIsInstance(send.call_args.kwargs['data'], bytes)
            self.assertNotIn(b'new GitHub', send.call_args.kwargs['data'])
            self.assertTrue(any(key.lower() == 'authorization' for key in send.call_args.kwargs['headers']))
        self.assertEqual(self.push.status()['errors'], [])

    def test_background_runs_without_browser_and_leaves_new_workspace_for_import(self):
        self.login()
        stop = threading.Event()
        with patch.object(core.NOTIFICATIONS, 'poll', side_effect=lambda: stop.set()) as poll, patch.object(self.push, 'flush') as push, patch.object(core, 'sync_github') as sync:
            cloud.run_background(self.web, stop)
            poll.assert_called_once(); push.assert_called_once(); sync.assert_not_called()

    def test_configuration_fails_closed(self):
        for values in ({'WORKDESK_PUBLIC_URL': 'http://public.example'}, {'WORKDESK_OWNER_ID': ''},
                       {'WORKDESK_SECRET_KEY': 'short'}, {'WORKDESK_DATA_DIR': ''}):
            with patch.dict(os.environ, values), self.assertRaises(ValueError):
                cloud.configuration()
