from contextlib import closing
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
from local_backups import Backups


class LocalAPI(app.WorkdeskAPI):
    local_storage = True

    def __init__(self, path, payload=None):
        self.path, self.payload = path, payload or {}

    def allowed_host(self):
        return True

    def read_payload(self):
        return self.payload

    def send_json(self, data, code=200):
        return data, code


class LocalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'WORKDESK_DATA_DIR': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        provider = patch.object(app, 'HOSTED_TOKEN_PROVIDER', None)
        provider.start(); self.addCleanup(provider.stop)
        app.init_db()
        self.backups = Backups(app.db_conn, app.default_data_dir)
        self.now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)

    def test_complete_snapshot_includes_committed_wal_and_all_saved_tables(self):
        task = app.save_manual({'title': 'A saved task', 'notes': 'Private note', 'priority': 1})
        LocalAPI('/api/settings', {'theme': 'dracula', 'contribution_emails': ['me@example.com']}).do_POST()
        with closing(app.db_conn()) as live:
            with live:
                live.execute('''INSERT INTO contributions VALUES
                    ('sample-user','pr:org/repo#1','org/repo','pr','{}','Shared contribution note',1,NULL)''')
                live.execute('''INSERT INTO notifications VALUES
                    ('sample-user','1','A notification','org/repo','PullRequest','mention',
                    'https://github.com/org/repo/pull/1',?,'revision',0,1)''', (app.utcnow(),))
                live.execute('UPDATE tasks SET notes=? WHERE id=?', ('Latest committed note', task['id']))
            result = self.backups.create()
            with closing(sqlite3.connect(result['path'])) as copy:
                self.assertEqual(copy.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(copy.execute('SELECT notes FROM tasks').fetchone()[0], 'Latest committed note')
                self.assertEqual(copy.execute('SELECT notes FROM contributions').fetchone()[0], 'Shared contribution note')
                self.assertEqual(copy.execute('SELECT unread FROM notifications').fetchone()[0], 0)
                settings = dict(copy.execute('SELECT key,value FROM settings'))
                self.assertEqual(json.loads(settings['theme']), 'dracula')
                self.assertTrue(json.loads(settings['theme_configured']))
                self.assertEqual(json.loads(settings['contribution_emails']), ['me@example.com'])
                original_tables = {r[0] for r in live.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                copied_tables = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(original_tables, copied_tables)
            self.assertFalse(Path(result['path'] + '-wal').exists())

    def test_automatic_backup_due_time_survives_restart(self):
        first = self.backups.create(automatic=True, clock=self.now)
        another = Backups(app.db_conn, app.default_data_dir)
        self.assertEqual(another.create(automatic=True, clock=self.now + timedelta(hours=23))['status'], 'not_due')
        second = another.create(automatic=True, clock=self.now + timedelta(days=1))
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(another.info()['backup_count'], 2)

    def test_retention_keeps_seven_automatic_and_every_manual_copy(self):
        manual = self.backups.create(clock=self.now)
        unrelated = self.backups.directory / 'my-important-backup.sqlite3'
        unrelated.write_bytes(b'keep this file')
        for day in range(10):
            self.backups.create(automatic=True, clock=self.now + timedelta(days=day))
        records = self.backups.inventory()
        self.assertEqual(len([r for r in records if r['kind'] == 'auto']), 7)
        self.assertTrue(Path(manual['path']).is_file())
        self.assertEqual(unrelated.read_bytes(), b'keep this file')

    def test_failed_backup_keeps_previous_copy_and_main_database(self):
        item = app.save_manual({'title': 'Keep me', 'notes': 'still here'})
        old = self.backups.create()
        with patch.object(self.backups, 'db_conn', side_effect=OSError('Simulated disk problem')):
            with self.assertRaises(OSError):
                self.backups.create()
        self.assertTrue(Path(old['path']).exists())
        self.assertEqual(self.backups.info()['backup_count'], 1)
        self.assertIn('disk problem', self.backups.info()['error'])
        self.assertEqual(list(self.backups.directory.glob('*.partial')), [])
        app.update_task(item['id'], {'notes': 'Normal saves still work'})
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT notes FROM tasks').fetchone()[0], 'Normal saves still work')
        self.backups.create()
        self.assertIsNone(self.backups.info()['error'])

    def test_cross_process_lock_prevents_overlapping_backup(self):
        other = Backups(app.db_conn, app.default_data_dir)
        with self.backups.process_lock():
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                other.create()
        self.assertEqual(other.create()['status'], 'saved')

    def test_theme_is_validated_and_persists_after_database_reopen(self):
        result, code = LocalAPI('/api/settings', {'theme': 'nord'}).do_POST()
        self.assertEqual(code, 200)
        app.init_db()
        with app.db_conn() as con:
            settings = app.get_settings(con)
            self.assertEqual(settings['theme'], 'nord')
            self.assertTrue(settings['theme_configured'])
        for bad in ('not-a-theme', ['nord'], None):
            _, code = LocalAPI('/api/settings', {'theme': bad}).do_POST()
            self.assertEqual(code, 400)

    def test_local_backup_api_and_hosted_guard(self):
        with patch.object(app, 'BACKUPS', self.backups):
            result, code = LocalAPI('/api/local-backup').do_POST()
            self.assertEqual(code, 200)
            self.assertTrue(Path(result['path']).is_file())
            for path in ('/api/local-backup', '/api/open-data-folder'):
                remote = LocalAPI(path); remote.local_storage = False
                _, code = remote.do_POST()
                self.assertEqual(code, 404)

    def test_open_folder_uses_only_the_configured_data_directory(self):
        with patch.object(app.sys, 'platform', 'darwin'), patch.object(app.subprocess, 'run') as run:
            _, code = LocalAPI('/api/open-data-folder', {'path': '/unrelated'}).do_POST()
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args.args[0], ['open', str(Path(self.temp.name).resolve())])

    def test_cli_backup_does_not_access_github(self):
        with patch('sys.argv', ['app.py', 'backup']), patch.object(app, 'github_token', side_effect=AssertionError('No network needed')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(app.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'saved')

    def test_automatic_failure_does_not_abort_the_caller(self):
        with patch.object(self.backups, 'create', side_effect=OSError('Disk full')):
            self.assertEqual(self.backups.maybe_create(), {'status': 'error'})

    def test_dashboard_reports_existing_local_snapshot(self):
        self.backups.create()
        report = app.dashboard({})['storage']
        self.assertEqual(report['mode'], 'local')
        self.assertEqual(report['backup_count'], 1)
        self.assertEqual(report['backup_directory'], str(self.backups.directory))
        self.assertEqual(report['automatic_copies'], 7)


if __name__ == '__main__':
    unittest.main()
