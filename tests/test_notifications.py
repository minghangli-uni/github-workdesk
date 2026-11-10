import io
import json
import os
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import notifications as n


def thread(identifier='1', updated='2026-11-10T00:00:00Z', **overrides):
    return {'id': identifier, 'updated_at': updated, 'unread': True, 'reason': 'mention',
            'repository': {'full_name': 'example/demo'},
            'subject': {'title': 'Please review this change', 'type': 'PullRequest',
                        'url': 'https://api.github.com/repos/example/demo/pulls/42',
                        'latest_comment_url': 'https://api.github.com/repos/example/demo/issues/comments/11'}, **overrides}


class FakeClient:
    interval = 60
    def __init__(self, items=None, username='sample-user'):
        self.items = items or []
        self.username = username
        self.accounts = []
    def get(self, route):
        assert route == '/user'
        return {'login': self.username}, {}
    def fetch(self, account):
        self.accounts.append(dict(account) if account else None)
        return self.items, {}


class TestNotifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'WORKDESK_DATA_DIR': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        app.init_db()
        self.clock = datetime(2026, 11, 10, 0, 1, tzinfo=timezone.utc)
        clock = patch.object(n, 'now', side_effect=lambda: self.clock)
        clock.start(); self.addCleanup(clock.stop)
        alert = patch.object(n, 'desktop_alert')
        self.alert = alert.start(); self.addCleanup(alert.stop)
        self.monitor = n.Monitor(app.db_conn, lambda: 'test-token')

    def advance(self, seconds=120):
        self.clock += timedelta(seconds=seconds)

    def setting(self, key, value):
        with app.db_conn() as con:
            con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))

    def test_baseline_is_quiet_then_new_and_changed_threads_alert_once(self):
        client = FakeClient([thread()])
        self.assertEqual(self.monitor.poll(client)['new'], 0)
        self.assertEqual(self.monitor.snapshot()['unread'], 1)
        self.assertEqual(self.alert.call_args.args[0], [])
        self.advance()
        client.items = [thread(), thread('2')]
        self.assertEqual(self.monitor.poll(client)['new'], 1)
        self.assertEqual(len(self.alert.call_args.args[0]), 1)
        self.advance()
        # Simulate an app restart: deduplication must live in SQLite.
        other = n.Monitor(app.db_conn, lambda: 'test-token')
        self.assertEqual(other.poll(client)['new'], 0)
        old = other.snapshot()['items']
        other.mark_read({'username': 'sample-user', 'items': old})
        self.assertEqual(other.snapshot()['unread'], 0)
        self.advance()
        client.items = [thread(updated='2026-11-10T00:06:00Z', unread=False)]
        self.assertEqual(other.poll(client)['new'], 1)
        self.assertEqual(other.snapshot()['unread'], 1)
        # Marking the old revision read cannot consume a new reply.
        other.mark_read({'username': 'sample-user', 'items': old})
        self.assertEqual(other.snapshot()['unread'], 1)

    def test_disabled_and_not_due_never_contact_github(self):
        self.setting('notifications_enabled', False)
        client = Mock()
        self.assertEqual(self.monitor.poll(client)['status'], 'disabled')
        client.get.assert_not_called()
        self.setting('notifications_enabled', True)
        good = FakeClient()
        good.interval = 180
        self.monitor.poll(good)
        self.advance(90)
        self.assertEqual(self.monitor.poll(client)['status'], 'waiting')
        client.get.assert_not_called()
        self.advance(91)
        self.assertEqual(self.monitor.poll(good)['status'], 'success')

    def test_failure_preserves_cursor_inbox_and_backs_off(self):
        self.monitor.poll(FakeClient([thread()]))
        self.advance()
        client = FakeClient()
        client.fetch = Mock(side_effect=n.APIError('Rate limited', 900))
        self.assertEqual(self.monitor.poll(client)['status'], 'error')
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot['unread'], 1)
        self.assertEqual(snapshot['status']['error'], 'Rate limited')
        self.advance(600)
        self.assertEqual(self.monitor.poll(client)['status'], 'waiting')
        self.advance(301)
        client = FakeClient([thread('2')])
        self.assertEqual(self.monitor.poll(client)['new'], 1)
        self.assertEqual(client.accounts[0]['cursor'], '2026-11-10T00:01:00Z')
        self.assertIsNone(self.monitor.snapshot()['status']['error'])

    def test_accounts_have_independent_baselines_and_inboxes(self):
        self.monitor.poll(FakeClient([thread()], 'alice'))
        self.advance()
        self.assertEqual(self.monitor.poll(FakeClient([thread('2')], 'bob'))['new'], 0)
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot['username'], 'bob')
        self.assertEqual([i['thread_id'] for i in snapshot['items']], ['2'])
        with self.assertRaises(ValueError):
            self.monitor.mark_read({'username': 'alice', 'items': snapshot['items']})
        self.setting('github_username', 'alice')
        self.advance()
        result = self.monitor.poll(FakeClient([], 'bob'))
        self.assertEqual(result['status'], 'error')
        self.assertIn('configured for alice', result['error'])

    def test_desktop_disabled_still_collects_inbox(self):
        self.monitor.poll(FakeClient())
        self.alert.reset_mock()
        self.setting('desktop_notifications', False)
        self.advance()
        self.monitor.poll(FakeClient([thread()]))
        self.alert.assert_not_called()
        self.assertEqual(self.monitor.snapshot()['unread'], 1)

    def test_cross_process_lock_does_not_duplicate_or_skip_updates(self):
        if os.name == 'nt':
            self.skipTest('Unix lock probe; Windows uses msvcrt locking')
        import fcntl
        with open(Path(self.temp.name) / 'workdesk.sqlite3.notifications.lock', 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            client = Mock()
            self.assertEqual(self.monitor.poll(client)['status'], 'busy')
            client.get.assert_not_called()
        self.assertEqual(self.monitor.poll(FakeClient())['status'], 'success')

    def test_malformed_page_does_not_advance_checkpoint(self):
        self.monitor.poll(FakeClient())
        self.advance()
        self.assertEqual(self.monitor.poll(FakeClient([thread('2'), {'id':'bad'}]))['status'], 'error')
        self.assertEqual(self.monitor.snapshot()['items'], [])
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT cursor FROM notification_accounts').fetchone()[0], '2026-11-10T00:01:00Z')

    def test_initial_query_validator_not_reused_for_incremental_query(self):
        client = FakeClient()
        client.fetch = Mock(return_value=([], {'Last-Modified': 'Tue, 10 Nov 2026 00:00:00 GMT', 'Date': 'Tue, 10 Nov 2026 00:00:30 GMT'}))
        self.monitor.poll(client)
        self.advance()
        followup = FakeClient()
        self.monitor.poll(followup)
        self.assertIsNone(followup.accounts[0]['last_modified'])
        self.assertEqual(followup.accounts[0]['cursor'], '2026-11-10T00:00:30Z')


class TestNotificationAPI(unittest.TestCase):
    def test_pagination_and_incremental_parameters(self):
        client = n.Client('not-a-real-token')
        client.get = Mock(side_effect=[([thread()], {'Link': '<https://api.github.com/notifications?page=2>; rel="next"'}), ([thread('2')], {})])
        account = {'cursor':'2026-11-10T01:00:00Z', 'last_modified':'date-header'}
        rows, _ = client.fetch(account)
        self.assertEqual(len(rows), 2)
        first, second = client.get.call_args_list
        self.assertEqual(first.args[1]['per_page'], 50)
        self.assertEqual(first.args[1]['participating'], 'true')
        self.assertEqual(first.args[1]['all'], 'true')
        self.assertEqual(first.args[1]['since'], '2026-11-10T00:59:00Z')
        self.assertEqual(first.args[2], 'date-header')
        self.assertIsNone(second.args[2])

    def test_304_and_rate_limit_headers(self):
        client = n.Client('not-a-real-token')
        headers = Message(); headers['X-Poll-Interval'] = '180'
        with patch.object(n, 'urlopen', side_effect=HTTPError('https://api.github.com/notifications',304,'Not modified',headers,io.BytesIO())):
            self.assertIsNone(client.get('/notifications')[0])
            self.assertEqual(client.interval, 180)
        headers['Retry-After'] = '900'
        with patch.object(n, 'urlopen', side_effect=HTTPError('https://api.github.com/notifications',403,'Forbidden',headers,io.BytesIO())):
            with self.assertRaises(n.APIError) as caught:
                client.get('/notifications')
        self.assertEqual(caught.exception.delay, 900)
        self.assertIn('fine-grained tokens', str(caught.exception))

    def test_safe_link_and_native_notification_arguments(self):
        payload = thread(subject={'title':'Untrusted " text \\ end run', 'type':'Issue', 'url':'javascript:alert(1)'})
        item = n.normalize(payload)
        self.assertEqual(item['url'], 'https://github.com/notifications')
        with patch.object(n.sys, 'platform', 'darwin'), patch.object(n.shutil, 'which', return_value='/usr/bin/osascript'), patch.object(n.subprocess, 'run') as run:
            n.desktop_alert([item])
        self.assertNotIn(item['title'], run.call_args.args[0][2])
        self.assertIn(item['title'], run.call_args.args[0][-1])

    def test_watcher_configuration_contains_no_token(self):
        import plistlib
        with tempfile.TemporaryDirectory() as folder, patch.object(app.sys, 'platform', 'darwin'), patch.object(app, 'watcher_path', return_value=Path(folder)/'watcher.plist'), patch.dict(os.environ, {'WORKDESK_DATA_DIR': folder, 'GH_TOKEN':'secret-test-token'}), patch.object(app.subprocess, 'run', return_value=Mock(returncode=0)):
            app.init_db()
            app.install_watcher()
            config = plistlib.loads((Path(folder)/'watcher.plist').read_bytes())
            self.assertEqual(config['StartInterval'], 60)
            self.assertEqual(config['ProgramArguments'][-1], 'check-notifications')
            self.assertNotIn('secret-test-token', str(config))
            app.remove_watcher()
            self.assertFalse((Path(folder)/'watcher.plist').exists())


if __name__ == '__main__':
    unittest.main()
