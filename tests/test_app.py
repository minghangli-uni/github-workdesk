import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class TestWorkdesk(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'WORKDESK_DATA_DIR': self.temp.name})
        self.env.start()
        contribution_sync = patch("app.contributions.sync", return_value=[])
        contribution_sync.start()
        self.addCleanup(contribution_sync.stop)
        app.init_db()
        with app.db_conn() as con:
            settings = app.get_settings(con)
            self.assertEqual(settings['github_username'], '')
            self.assertEqual(settings['focus_orgs'], [])

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_manual_tasks_persist_and_edit(self):
        x = app.save_manual({'title':'Write release notes', 'priority':1,'notes':'test', 'due_date':'2026-12-01', 'repo':'example-org'})
        self.assertEqual(x['source'],'manual')
        self.assertEqual(x['notes'],'test')
        self.assertEqual(x['priority'],1)
        with app.db_conn() as con:
            self.assertEqual(app.summary_counts(con)['manual_open'],1)
        app.update_task(x['id'], {'local_status':'done', 'notes':'finished'})
        with app.db_conn() as con:
            self.assertEqual(app.summary_counts(con)['manual_open'],0)
            self.assertEqual(app.summary_counts(con)['completed'],1)
            self.assertIsNotNone(con.execute('SELECT closed_at FROM tasks WHERE id=?',(x['id'],)).fetchone()[0])
            self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],2)

    def test_merge_keeps_personal_notes(self):
        data = {'id':'gh:example-org/demo-repo#88','kind':'pr','title':'Test PR',
                'description':'body','repo':'example-org/demo-repo',
                'url':'https://github.com/example-org/demo-repo/pull/88',
                'github_state':'open','is_draft':0,
                'created_at':'2026-10-01T00:00:00Z','updated_at':'2026-11-01T00:00:00Z',
                'closed_at':None,'labels':'[]'}
        with app.db_conn() as con:
            self.assertEqual(app.merge_gh_task(con, data, 'author', app.utcnow()),'new')
        app.update_task(data['id'], {'priority':1,'notes':'Private note','due_date':'2026-11-11'})
        with app.db_conn() as con:
            app.merge_gh_task(con, {**data,'github_state':'merged','updated_at':'2026-11-10T02:00:00Z'}, 'author', app.utcnow())
            row = con.execute('SELECT * FROM tasks WHERE id=?',(data['id'],)).fetchone()
            self.assertEqual(row['notes'],'Private note')
            self.assertEqual(row['priority'],1)
            self.assertEqual(row['due_date'],'2026-11-11')
            self.assertEqual(row['github_state'],'merged')
            self.assertEqual(app.summary_counts(con)['completed'],1)

    def test_multiple_roles_and_review_removed_historical(self):
        data = {'id':'gh:someorg/repo#10','kind':'pr','title':'Review',
                'description':'','repo':'someorg/repo','url':'https://github.com/someorg/repo/pull/10',
                'github_state':'open','is_draft':0,'created_at':app.utcnow(),
                'updated_at':app.utcnow(),'closed_at':None,'labels':'[]'}
        with app.db_conn() as con:
            app.merge_gh_task(con,data,'review',app.utcnow())
            self.assertEqual(app.summary_counts(con)['reviews'],1)
            con.execute('UPDATE tasks SET role_review=0 WHERE id=?',(data['id'],))
            self.assertEqual(app.summary_counts(con)['reviews'],0)
            self.assertEqual(app.summary_counts(con)['completed'],1)

    def test_different_authenticated_account_is_supported(self):
        with app.db_conn() as con:
            self.assertEqual(app.get_settings(con)['github_username'], '')
        class Client:
            def get(self, route, params=None):
                return {'login': 'another-user'}
            def search(self, query):
                self_outer.assertIn('another-user', query)
                return [], True
        self_outer = self
        self.assertEqual(app.sync_github(Client())['status'], 'success')
        with app.db_conn() as con:
            self.assertEqual(app.get_settings(con)['last_synced_user'], 'another-user')

    def test_explicit_account_setting_prevents_mismatched_sync(self):
        with app.db_conn() as con:
            con.execute("UPDATE settings SET value=? WHERE key='github_username'", ('"my-account"',))
        class Client:
            def get(self, route, params=None):
                return {'login': 'different-account'}
            def search(self, query):
                raise AssertionError('Search must not proceed with mismatched login')
        with self.assertRaisesRegex(RuntimeError, 'different-account'):
            app.sync_github(Client())

    def test_no_user_specific_defaults(self):
        with app.db_conn() as con:
            settings = app.get_settings(con)
        self.assertEqual(settings['github_username'], '')
        self.assertEqual(settings['focus_orgs'], [])
        self.assertEqual(settings['theme'], 'system')

    def test_validation(self):
        for payload in ({'title':''},{'title':'test','url':'javascript:alert(1)'},{'title':'x','priority':7},{'title':'x','due_date':'2026-99-99'}):
            with self.assertRaises(ValueError):
                app.save_manual(payload)

    def test_sync_fake_client(self):
        class Client:
            def get(self, route, params=None):
                if route == '/user': return {'login':'sample-user'}
                raise AssertionError(route)
            def search(self, query):
                if query.startswith('is:pr author:') and 'is:open' in query:
                    return ([{'number':104,'title':'Open PR','body':'',
                              'repository_url':'https://api.github.com/repos/example-org/demo-repo',
                              'html_url':'https://github.com/example-org/demo-repo/pull/104',
                              'state':'open','draft':False,'created_at':app.utcnow(),'updated_at':app.utcnow(),
                              'pull_request':{},'labels':[]}],True)
                return [],True
        result=app.sync_github(Client())
        with app.db_conn() as con:
            self.assertEqual(app.get_settings(con)['last_synced_user'], 'sample-user')
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['counts']['open_prs'],1)
        self.assertEqual(result['added'],1)
        self.assertEqual(app.sync_github(Client())['added'],0)
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM snapshots').fetchone()[0],2)


if __name__ == '__main__':
    unittest.main()
