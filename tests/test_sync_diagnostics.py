import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import contributions as c
import sync_diagnostics as d
from test_contributions import Client, PROFILE

EMAIL_NOTE='Verified email lookup unavailable; co-author matching uses public, noreply and configured emails.'
REPO_NOTE='Open-PR scans limited to 30 repositories. Extra repositories in Settings take priority.'
TIMEOUT='Review evidence for example/shared#42: The read operation timed out'


class TestDiagnosticMigration(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:')
        self.con.row_factory=sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript('''CREATE TABLE sync_runs(id INTEGER PRIMARY KEY,status TEXT,errors TEXT);
            CREATE TABLE contribution_sync(username TEXT PRIMARY KEY,checked_at TEXT,errors TEXT,requests INTEGER,repositories INTEGER);''')

    def row(self,identifier,status,messages):
        self.con.execute('INSERT INTO sync_runs VALUES (?,?,?)',(identifier,status,json.dumps(messages)))

    def test_coverage_only_history_becomes_limited_without_losing_messages(self):
        self.row(1,'partial',['Contributions: '+EMAIL_NOTE,'Contributions: '+REPO_NOTE])
        d.migrate(self.con,'sync_runs')
        result=dict(self.con.execute('SELECT * FROM sync_runs').fetchone())
        self.assertEqual(result['status'],'limited')
        self.assertEqual(json.loads(result['errors']),[])
        self.assertEqual(json.loads(result['warnings']),[EMAIL_NOTE,REPO_NOTE])
        d.migrate(self.con,'sync_runs')
        self.assertEqual(dict(self.con.execute('SELECT * FROM sync_runs').fetchone()),result)

    def test_mixed_history_preserves_timeout_unknown_errors_and_failed_status(self):
        self.row(1,'partial',['Contributions: '+EMAIL_NOTE,'Contributions: '+TIMEOUT,'Unexpected failure'])
        self.row(2,'failed',['Contributions: '+EMAIL_NOTE])
        d.migrate(self.con,'sync_runs')
        result=dict(self.con.execute('SELECT * FROM sync_runs WHERE id=1').fetchone())
        self.assertEqual(result['status'],'partial')
        self.assertEqual(json.loads(result['errors']),['Contributions: '+TIMEOUT,'Unexpected failure'])
        self.assertEqual(json.loads(result['warnings']),[EMAIL_NOTE])
        self.assertEqual(self.con.execute('SELECT status FROM sync_runs WHERE id=2').fetchone()[0],'failed')

    def test_core_search_problems_are_not_reclassified_as_contribution_notes(self):
        self.row(1,'partial',[REPO_NOTE,'Search incomplete: role review','Contributions: GitHub rate-limited contribution discovery.'])
        d.migrate(self.con,'sync_runs')
        result=self.con.execute('SELECT * FROM sync_runs').fetchone()
        self.assertEqual(len(json.loads(result['errors'])),3)
        self.assertEqual(json.loads(result['warnings']),[])

    def test_contribution_migration_preserves_request_counts_and_all_reasons(self):
        self.con.execute('INSERT INTO contribution_sync VALUES (?,?,?,?,?)',('sample-user','2026-11-11T00:00:00Z',json.dumps([EMAIL_NOTE,REPO_NOTE,TIMEOUT]),362,79))
        d.migrate(self.con,'contribution_sync')
        result=self.con.execute('SELECT * FROM contribution_sync').fetchone()
        self.assertEqual(json.loads(result['errors']),[TIMEOUT])
        self.assertEqual(json.loads(result['warnings']),[EMAIL_NOTE,REPO_NOTE])
        self.assertEqual(result['requests'],362)
        self.assertEqual(result['repositories'],79)

    def test_all_known_limits_but_not_generic_exception_text_are_notes(self):
        notes=[EMAIL_NOTE,REPO_NOTE,'PR association checks limited to 100 commits this sync.',
               'Contribution scan limited to 200 PRs.', 'Contribution scan reached its request limit; saved results are retained.',
               'Incomplete contribution list: /fixture.', 'PR example/shared#1 exceeds GitHub’s 250-commit PR limit; available evidence saved.',
               'Repository discovery was limited to 100 recently pushed repositories.',
               'Co-author search was limited to 15 email identities.',
               'GitHub returned incomplete contribution search results.',
               'A contribution search exceeded GitHub’s 1,000-result limit.']
        self.assertTrue(all(d.legacy_coverage_note(note) for note in notes))
        self.assertFalse(d.legacy_coverage_note(TIMEOUT))
        self.assertFalse(d.legacy_coverage_note('Unexpected value: Open-PR scans limited to 30'))


class TestSyncOutcomes(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        env=patch.dict(os.environ,{'WORKDESK_DATA_DIR':directory.name});env.start();self.addCleanup(env.stop)
        app.init_db()
        self.client=Client()
        self.client.search=lambda query:([],True)
        with app.db_conn() as con:self.settings=app.get_settings(con)

    def scan(self):
        return c.Scanner(app.db_conn,self.client,PROFILE,self.settings,'2026-01-01').run()

    def test_missing_optional_email_access_is_a_note_but_timeout_is_a_failure(self):
        get=self.client.get
        def missing(route,params=None):
            if route=='/user/emails':raise app.GitHubAPIError(404,'Not Found')
            return get(route,params)
        self.client.get=missing
        report=self.scan()
        self.assertFalse(report['errors'])
        self.assertIn('Optional email access',report['warnings'][0])
        def timeout(route,params=None):
            if route=='/user/emails':raise TimeoutError('The read operation timed out')
            return get(route,params)
        self.client.get=timeout
        report=self.scan()
        self.assertIn('timed out',report['errors'][0])
        self.assertFalse(any('Optional email' in message for message in report['warnings']))

    def test_invalid_authentication_and_rate_limit_are_real_failures(self):
        for exception in (app.GitHubAPIError(401,'Bad credentials'),RuntimeError('GitHub API rate limit exceeded')):
            with self.subTest(error=str(exception)):
                self.client.get=Mock(side_effect=exception)
                report=self.scan()
                self.assertTrue(report['errors'])
                self.assertFalse(report['warnings'])
        self.assertEqual(self.client.get.call_count,1)

    def test_request_budget_is_a_note_and_stops_api_requests(self):
        with patch.object(c,'MAX_REQUESTS',0):report=self.scan()
        self.assertFalse(report['errors'])
        self.assertIn('request limit',report['warnings'][0])
        self.assertEqual(self.client.calls,[])

    def test_sync_result_database_and_error_state_agree_for_all_outcomes(self):
        for errors,warnings,status in [([],[],'success'),([],[REPO_NOTE],'limited'),([TIMEOUT],[REPO_NOTE],'partial')]:
            with self.subTest(status=status), patch.object(c,'sync',return_value={'errors':errors,'warnings':warnings}):
                result=app.sync_github(self.client)
                self.assertEqual(result['status'],status)
                self.assertEqual(result['warnings'],warnings)
                self.assertEqual(bool(app.SYNC_STATE['error']),bool(errors))
                self.assertEqual(app.SYNC_STATE['warnings'],warnings)
                latest=app.dashboard({})['runs'][0]
                self.assertEqual(latest['status'],status)
                self.assertEqual(latest['warnings'],warnings)
                self.assertEqual(latest['errors'],result['errors'])

    def test_cli_limits_are_successful_and_real_errors_have_nonzero_exit(self):
        for status,code in [('limited',0),('success',0),('partial',2)]:
            with self.subTest(status=status), patch.object(sys,'argv',['app.py','sync']),patch.object(app,'sync_github',return_value={'status':status}),patch('builtins.print'):
                self.assertEqual(app.main(),code)

    def test_schema_upgrade_does_not_change_private_tasks_or_contributions(self):
        task=app.save_manual({'title':'Keep this task','notes':'Keep this private note'})
        self.scan()
        with app.db_conn() as con:
            before=[tuple(row) for row in con.execute('SELECT * FROM contributions')]
        app.init_db()
        with app.db_conn() as con:
            self.assertEqual(con.execute('SELECT notes FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],'Keep this private note')
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM contributions')],before)


if __name__=='__main__':unittest.main()
