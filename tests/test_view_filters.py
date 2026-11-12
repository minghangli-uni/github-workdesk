import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import view_filters


class TestSavedFilters(unittest.TestCase):
    def test_accepts_distinct_roles_statuses_and_view_scopes(self):
        value = {'tables': {'overview': {'status':'merged','role':'coauthor'}, 'prs': {'status':'closed_unmerged'},
                            'tasks': {'status':'in_progress','from':'2026-11-13'}},
                 'contributions': {'tab':'history','status':'committed','discussion':False}}
        self.assertEqual(view_filters.validate_saved(value), value)

    def test_rejects_bad_shapes_unknown_fields_and_values(self):
        for value in (None, [], {'extra':{}}, {'tables':{'unknown':{}}}, {'tables':{'prs':None}},
                      {'tables':{'prs':{'status':'<script>'}}}, {'tables':{'prs':{'role':[]}}},
                      {'contributions':{'discussion':'false'}}, {'contributions':{'query':'x'*301}},
                      {'tables':{'prs':{'from':'2026-02-31'}}}, {'tables':{'prs':{'to':'20261113'}}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                view_filters.validate_saved(value)

    def test_restarting_preserves_filters_and_work_records(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'WORKDESK_DATA_DIR':folder}):
            app.init_db()
            task = app.save_manual({'title':'Keep my work','notes':'Unchanged private note'})
            value = view_filters.validate_saved({'tables':{'prs':{'status':'merged','role':'reviewer'}}})
            with app.db_conn() as con:
                con.execute("UPDATE settings SET value=? WHERE key='view_filters'", (json.dumps(value),))
            app.init_db()
            with app.db_conn() as con:
                self.assertEqual(app.get_settings(con)['view_filters'],value)
                self.assertEqual(con.execute('SELECT notes FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],'Unchanged private note')
