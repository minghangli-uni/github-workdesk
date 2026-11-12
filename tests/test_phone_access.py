import copy
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import phone_access as phone

CONFIG = {'enabled': True, 'host': 'my-mac.example.ts.net', 'owner': 'owner@example.com'}
HOST = phone.authority(CONFIG)
ORIGIN = 'https://' + HOST
SERVE = {'TCP': {'8443': {'HTTPS': True}},
         'Web': {HOST: {'Handlers': {'/': {'Proxy': phone.TARGET}}}}}


class TestPhoneHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {'WORKDESK_DATA_DIR': cls.temp.name})
        cls.env.start()
        app.init_db()
        cls.access = phone.Access(app.default_data_dir, app.WorkdeskAPI)
        cls.access.config = CONFIG.copy()
        handler = phone.gateway_handler(app.WorkdeskAPI, cls.access)
        handler.log_message = lambda *args: None
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.env.stop()
        cls.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        sent = {'Host': HOST, 'Tailscale-User-Login': CONFIG['owner'],
                'Content-Type': 'application/json', 'Origin': ORIGIN}
        sent.update(headers or {})
        sent = {k: v for k, v in sent.items() if v is not None}
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None, headers=sent)
            response = conn.getresponse()
            content = response.read()
            data = json.loads(content) if 'application/json' in response.getheader('Content-Type', '') else content
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def test_owner_reads_dashboard_and_installs_home_screen_assets(self):
        status, data, headers = self.request('GET', '/api/dashboard')
        self.assertEqual(status, 200)
        self.assertEqual(data['phone_access'], {'remote': True, 'url': ORIGIN})
        self.assertEqual(data['storage']['database_path'], str(Path(self.temp.name).resolve() / 'workdesk.sqlite3'))
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        for path in ['/', '/manifest.webmanifest', '/sw.js', '/static/offline.html', '/static/phone-access.js']:
            self.assertEqual(self.request('GET', path)[0], 200, path)

    def test_missing_or_wrong_identity_cannot_read_any_private_endpoint(self):
        for identity in (None, '', 'someone@example.com'):
            for path in ('/', '/api/dashboard', '/api/notifications', '/api/export', '/api/backup', '/manifest.webmanifest'):
                self.assertEqual(self.request('GET', path, headers={'Tailscale-User-Login': identity})[0], 403)

    def test_reject_wrong_host_and_identity_header_even_on_mutations(self):
        for headers in ({'Host': '127.0.0.1:8766'}, {'Host': 'evil.example'}, {'Host': CONFIG['host']}, {'Tailscale-User-Login': 'someone@example.com'}):
            for method in ('POST', 'PATCH', 'DELETE'):
                path = '/api/tasks' if method == 'POST' else '/api/tasks/no-such-task'
                self.assertEqual(self.request(method, path, {'title': 'Must not save'}, headers)[0], 403)

    def test_phone_saves_notes_in_same_mac_database(self):
        status, data, _ = self.request('POST', '/api/tasks', {'title': 'Phone task', 'notes': 'Private phone note'})
        self.assertEqual(status, 201)
        task_id = data['id']
        self.assertEqual(self.request('PATCH', '/api/tasks/' + task_id, {'notes': 'Updated from phone'})[0], 200)
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT notes FROM tasks WHERE id=?', (task_id,)).fetchone()[0], 'Updated from phone')
        self.assertEqual(self.request('DELETE', '/api/tasks/' + task_id, {})[0], 200)

    def test_csrf_cannot_create_modify_or_delete_work(self):
        task = app.save_manual({'title': 'Keep me', 'notes': 'Original'})
        for method in ('POST', 'PATCH', 'DELETE'):
            path = '/api/tasks' if method == 'POST' else '/api/tasks/' + task['id']
            for origin in (None, 'null', 'https://evil.example', 'http://' + HOST):
                self.assertEqual(self.request(method, path, {'title': 'Unwanted', 'notes': 'Unwanted'}, {'Origin': origin})[0], 400)
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT notes FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'Original')
            self.assertEqual(con.execute("SELECT COUNT(*) FROM tasks WHERE title='Unwanted'").fetchone()[0], 0)

    def test_reject_non_json_and_invalid_payload(self):
        for headers in ({'Content-Type': 'text/plain'}, {'Content-Length': '-1'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('POST', '/api/tasks', {'title': 'Not saved'}, headers)[0], 400)
        self.assertEqual(self.request('POST', '/api/tasks', ['not an object'])[0], 400)

    def test_remote_cannot_control_mac_or_phone_setup(self):
        for path in ('/api/phone-access', '/api/open-data-folder', '/api/notification-watcher', '/api/reminder'):
            self.assertEqual(self.request('POST', path, {'action': 'enable'})[0], 403, path)
        self.assertEqual(self.request('GET', '/api/phone-access')[0], 404)

    def test_disabling_identity_blocks_existing_connections(self):
        with patch.object(self.access, 'config', None):
            self.assertEqual(self.request('GET', '/api/dashboard')[0], 403)
            self.assertEqual(self.request('POST', '/api/tasks', {'title': 'Not saved'})[0], 403)


class TestPhoneSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.access = phone.Access(lambda: Path(self.temp.name), app.WorkdeskAPI)
        self.gateway = patch.object(self.access, 'start_gateway', side_effect=lambda: setattr(self.access, 'server', Mock()))
        self.gateway.start()
        self.addCleanup(self.gateway.stop)

    def test_enable_verifies_and_persists_private_route(self):
        with patch.object(phone, 'current_identity', return_value=CONFIG.copy()), patch.object(phone, 'run_cli', side_effect=[{}, None, SERVE]) as cli:
            info = self.access.change('enable')
        self.assertTrue(info['enabled'])
        self.assertTrue(info['verified'])
        self.assertEqual(info['url'], ORIGIN)
        self.assertEqual(cli.call_args_list[1].args, ('serve', '--bg', '--https=8443', '--yes', phone.TARGET))
        self.assertEqual(json.loads(self.access.config_path.read_text()), CONFIG)
        if os.name != 'nt':
            self.assertEqual(self.access.config_path.stat().st_mode & 0o777, 0o600)

    def test_existing_routes_and_foreground_routes_are_not_overwritten(self):
        for serve in (SERVE, {'TCP': {'8443': {'TCPForward': '127.0.0.1:9999'}}}, {'Foreground': {'session': SERVE}}):
            with patch.object(phone, 'current_identity', return_value=CONFIG), patch.object(phone, 'run_cli', return_value=serve) as cli:
                with self.assertRaisesRegex(ValueError, 'already in use'):
                    self.access.change('enable')
            self.assertEqual(cli.call_count, 1)
            self.assertFalse(self.access.config_path.exists())

    def test_failed_persistence_revokes_gateway_and_removes_only_our_route(self):
        with patch.object(phone, 'current_identity', return_value=CONFIG), patch.object(phone, 'run_cli', side_effect=[{}, None, SERVE, SERVE, None]) as cli, patch.object(self.access, 'save_config', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(ValueError, 'Disk full'):
                self.access.change('enable')
        self.assertIsNone(self.access.config)
        self.assertIsNone(self.access.server)
        self.assertEqual(cli.call_args.args, ('serve', '--https=8443', 'off'))

    def test_public_funnel_route_never_passes_verification(self):
        public = {**SERVE, 'AllowFunnel': {HOST: True}}
        with patch.object(phone, 'current_identity', return_value=CONFIG), patch.object(phone, 'run_cli', side_effect=[{}, None, public, public]):
            with self.assertRaisesRegex(ValueError, 'did not confirm a private'):
                self.access.change('enable')
        self.assertIsNone(self.access.config)
        self.assertFalse(self.access.config_path.exists())

    def test_disable_blocks_access_even_when_tailscale_unavailable(self):
        self.access.config = CONFIG.copy()
        self.access.server = Mock()
        with patch.object(phone, 'run_cli', side_effect=ValueError('Unavailable')):
            self.assertFalse(self.access.change('disable')['enabled'])
        self.assertIsNone(self.access.config)
        self.assertIsNone(self.access.server)
        self.assertEqual(json.loads(self.access.config_path.read_text()), {'enabled': False})

    def test_disable_leaves_changed_mapping_untouched(self):
        self.access.config = CONFIG.copy()
        changed = copy.deepcopy(SERVE)
        changed['Web'][HOST]['Handlers']['/']['Proxy'] = 'http://127.0.0.1:9999'
        with patch.object(phone, 'run_cli', return_value=changed) as cli:
            self.access.change('disable')
        self.assertEqual(cli.call_count, 1)

    def test_disable_never_removes_another_host_on_the_same_port(self):
        self.access.config = CONFIG.copy()
        changed = copy.deepcopy(SERVE)
        changed['Web']['another-mac.example.ts.net:8443'] = {'Handlers': {'/': {'Text': 'Other app'}}}
        with patch.object(phone, 'run_cli', return_value=changed) as cli:
            self.access.change('disable')
        self.assertEqual(cli.call_count, 1)

    def test_restart_restores_gate_but_not_external_network_configuration(self):
        self.access.save_config(CONFIG)
        with patch.object(phone, 'run_cli') as cli:
            self.access.start()
        self.assertEqual(self.access.config, CONFIG)
        self.assertFalse(cli.called)
        self.assertFalse(self.access.info()['verified'])

    def test_corrupted_config_fails_closed(self):
        self.access.config_path.write_text('{broken')
        self.access.start()
        self.assertIsNone(self.access.config)
        self.assertIsNone(self.access.server)

    def test_check_detects_account_change(self):
        self.access.config = CONFIG.copy()
        other = {**CONFIG, 'owner': 'another@example.com'}
        with patch.object(phone, 'current_identity', return_value=other), patch.object(phone, 'run_cli', return_value=SERVE):
            with self.assertRaisesRegex(ValueError, 'account or Mac address changed'):
                self.access.change('check')
        self.assertFalse(self.access.verified)

    def test_identity_requires_logged_in_untagged_device_and_safe_hostname(self):
        status = {'BackendState': 'Running', 'Self': {'DNSName': CONFIG['host'] + '.', 'UserID': 42}, 'User': {'42': {'LoginName': CONFIG['owner']}}}
        with patch.object(phone, 'run_cli', return_value=status):
            self.assertEqual(phone.current_identity(), CONFIG)
        for invalid in ({**status, 'BackendState': 'Stopped'}, {**status, 'Self': {**status['Self'], 'Tags': ['tag:server']}}, {**status, 'Self': {**status['Self'], 'DNSName': 'evil.example'}}, {**status, 'User': {}}):
            with patch.object(phone, 'run_cli', return_value=invalid):
                with self.assertRaises(ValueError):
                    phone.current_identity()

    def test_cli_forces_command_mode_and_does_not_leak_output(self):
        result = Mock(returncode=0, stdout='{}')
        with patch.object(phone, 'find_cli', return_value='/fake/Tailscale'), patch.object(phone.subprocess, 'run', return_value=result) as run:
            self.assertEqual(phone.run_cli('status', '--json'), {})
            self.assertEqual(run.call_args.kwargs['env']['TAILSCALE_BE_CLI'], '1')
            result.returncode = 1
            result.stdout = 'Private device details'
            with self.assertRaises(ValueError) as error:
                phone.run_cli('status', '--json')
            self.assertNotIn('Private device details', str(error.exception))


if __name__ == '__main__':
    unittest.main()
