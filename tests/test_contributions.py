import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import contributions as c
import notifications as n

PROFILE={'login':'sample-user','id':123,'email':'sample@example.com'}
DATE='2026-11-10T00:00:00Z'
REPO='example/shared'


def commit(sha='a'*40, author='sample-user', message='Implement shared feature', email='sample@example.com', committer='someone-else'):
    return {'sha':sha,'author':{'login':author},'committer':{'login':committer},
            'commit':{'message':message,'author':{'email':email,'date':DATE},'committer':{'email':'other@example.com','date':DATE}},'repository':{'full_name':REPO}}


def pr(number=1, **overrides):
    return {'number':number,'title':'Shared feature','user':{'login':'someone-else'},'state':'open',
            'updated_at':DATE,'created_at':'2026-10-01T00:00:00Z','merged_at':None,'draft':False,'commits':1,
            'head':{'sha':'head'},**overrides}


class Client:
    def __init__(self):
        self.prs={1:pr()}
        self.commits={1:[commit()]}
        self.reviews={1:[]}
        self.comments={1:[]}
        self.search_commits=[]
        self.associations={}
        self.fail=set()
        self.calls=[]
    def get(self, route, params=None):
        self.calls.append((route,params))
        if route in self.fail: raise RuntimeError('Fixture request failed')
        if route=='/user': return PROFILE
        if route=='/user/emails': return [{'email':'sample@example.com','verified':True}]
        if route=='/user/repos': return [{'full_name':REPO}]
        if route=='/search/issues': return {'items':[], 'total_count':0}
        if route=='/search/commits': return {'items':self.search_commits,'total_count':len(self.search_commits)}
        if route==f'/repos/{REPO}/pulls': return list(self.prs.values())
        if route.startswith(f'/repos/{REPO}/commits/') and route.endswith('/pulls'):
            return [self.prs[number] for number in self.associations.get(route.split('/')[-2],[])]
        for number, value in self.prs.items():
            if route==f'/repos/{REPO}/pulls/{number}': return value
            if route==f'/repos/{REPO}/pulls/{number}/commits': return self.commits.get(number,[])
            if route==f'/repos/{REPO}/pulls/{number}/reviews': return self.reviews.get(number,[])
            if route==f'/repos/{REPO}/issues/{number}/comments': return self.comments.get(number,[])
        raise AssertionError(route)


class TestContributions(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        env=patch.dict(os.environ,{'WORKDESK_DATA_DIR':folder.name});env.start();self.addCleanup(env.stop)
        app.init_db()
        with app.db_conn() as con:
            con.execute("INSERT OR REPLACE INTO settings VALUES ('last_synced_user',?)",(json.dumps(PROFILE['login']),))
            self.settings=app.get_settings(con)
        self.client=Client()

    def scan(self):
        scanner=c.Scanner(app.db_conn,self.client,PROFILE,self.settings,'2026-01-01')
        report=scanner.run()
        errors=report['errors']
        with app.db_conn() as con:
            data=c.snapshot(con)
        return data,errors

    def test_open_branch_commit_found_without_authored_pr_or_comment(self):
        data,errors=self.scan()
        self.assertEqual(errors,[])
        self.assertEqual(len(data['items']),1)
        item=data['items'][0]
        self.assertEqual(item['roles'],['code'])
        self.assertEqual(item['state'],'open')
        self.assertEqual(item['status'],'Awaiting review')
        self.assertEqual(item['evidence'][0]['sha'],'a'*40)
        self.assertEqual(item['last_contributed_at'],DATE)
        queries=[params['q'] for route,params in self.client.calls if route=='/search/issues']
        self.assertTrue(any('reviewed-by:' in q and 'is:open' in q and 'updated:' not in q for q in queries))
        self.assertTrue(any('commenter:' in q and 'is:open' in q and 'updated:' not in q for q in queries))

    def test_coauthor_matches_exact_email_in_trailer_not_name_or_body(self):
        identities=c.identities(PROFILE)
        raw=commit(author='other',email='other@example.com',message='Feature\n\nCo-authored-by: A Person <123+sample-user@users.noreply.github.com>')
        self.assertEqual(c.commit_evidence(raw,REPO,'sample-user',identities)['roles'],['coauthor'])
        for message in ['Feature\n\nCo-authored-by: sample-user <someone@example.com>', 'Example\n\nCo-authored-by: A Person <sample@example.com>\n\nThis is an example only.']:
            raw['commit']['message']=message
            self.assertIsNone(c.commit_evidence(raw,REPO,'sample-user',identities))

    def test_multiple_roles_one_pr_and_only_submitted_reviews_count(self):
        self.client.prs[1]=pr(user={'login':'sample-user'})
        self.client.reviews[1]=[{'id':4,'user':{'login':'sample-user'},'state':'APPROVED','submitted_at':DATE,'commit_id':'head'},
                                {'id':5,'user':{'login':'sample-user'},'state':'PENDING','submitted_at':None}]
        data,errors=self.scan()
        item=data['items'][0]
        self.assertEqual(item['roles'],['author','code','reviewer'])
        self.assertEqual(item['status'],'Reviewed')
        self.assertEqual(len(item['evidence']),3)
        self.assertFalse(errors)

    def test_squash_coauthor_credit_grouped_under_merged_pr(self):
        squash=commit('b'*40,author='other',email='other@example.com',message='Squash feature\n\nCo-authored-by: A Person <sample@example.com>')
        self.client.search_commits=[squash]
        self.client.associations[squash['sha']]=[1]
        self.client.commits[1]=[]
        self.client.prs[1]=pr(state='closed',merged_at=DATE)
        data,errors=self.scan()
        self.assertFalse(errors)
        self.assertEqual(len(data['items']),1)
        self.assertEqual(data['items'][0]['kind'],'pr')
        self.assertEqual(data['items'][0]['roles'],['coauthor'])
        self.assertEqual(data['items'][0]['state'],'merged')

    def test_direct_commit_retained_and_failed_association_is_labelled(self):
        self.client.prs={}
        self.client.search_commits=[commit()]
        data,errors=self.scan()
        self.assertEqual(data['items'][0]['kind'],'commit')
        self.assertEqual(data['items'][0]['status'],'Committed')
        self.client.fail.add(f'/repos/{REPO}/commits/{"a"*40}/pulls')
        data,errors=self.scan()
        self.assertTrue(errors)
        self.assertIn('unverified',data['items'][0]['status'])

    def test_notes_and_follow_preserved_across_sync_and_account_isolation(self):
        data,_=self.scan();item=data['items'][0]
        with app.db_conn() as con:
            c.update(con,{'username':'sample-user','id':item['id'],'notes':'Private context','followed':True})
        self.client.prs[1]=pr(title='Updated title',updated_at='2026-11-11T00:00:00Z')
        data,_=self.scan()
        self.assertEqual(data['items'][0]['notes'],'Private context')
        self.assertTrue(data['items'][0]['followed'])
        with app.db_conn() as con:
            con.execute("UPDATE settings SET value='\"other-user\"' WHERE key='github_username'")
            self.assertEqual(c.snapshot(con)['items'],[])
            with self.assertRaises(ValueError): c.update(con,{'username':'sample-user','id':item['id'],'notes':'wrong'})

    def test_partial_failure_and_force_push_keep_labelled_evidence(self):
        self.scan()
        self.client.prs[1]=pr(updated_at='2026-11-11T00:00:00Z')
        self.client.fail.add(f'/repos/{REPO}/pulls/1/commits')
        data,errors=self.scan()
        self.assertTrue(errors)
        self.assertEqual(len(data['items'][0]['evidence']),1)
        self.assertFalse(data['items'][0]['details_complete'])
        self.client.fail.clear();self.client.commits[1]=[]
        data,_=self.scan()
        self.assertTrue(data['items'][0]['evidence'][0]['historical'])

    def test_unchanged_pr_reuses_evidence_but_new_email_triggers_rescan(self):
        self.scan();self.client.calls=[]
        self.scan()
        self.assertFalse(any(route.endswith('/reviews') for route,_ in self.client.calls))
        self.settings['contribution_emails']=['old@example.net'];self.client.calls=[]
        self.scan()
        self.assertTrue(any(route.endswith('/reviews') for route,_ in self.client.calls))

    def test_committer_is_not_mislabelled_as_author(self):
        raw=commit(author='other',email='other@example.com',committer='sample-user')
        self.assertEqual(c.commit_evidence(raw,REPO,'sample-user',c.identities(PROFILE))['roles'],['committer'])

    def test_rebase_activity_does_not_change_personal_authorship_date(self):
        raw=commit()
        raw['commit']['committer']['date']='2026-11-12T00:00:00Z'
        evidence=c.commit_evidence(raw,REPO,'sample-user',c.identities(PROFILE))
        self.assertEqual(evidence['date'],DATE)
        self.assertEqual(evidence['activity_at'],'2026-11-12T00:00:00Z')

    def test_discussion_only_is_distinct_and_requested_review_is_not_contribution(self):
        self.client.commits[1]=[]
        self.assertEqual(self.scan()[0]['items'],[])
        self.client.comments[1]=[{'id':9,'user':{'login':'sample-user'},'created_at':DATE}]
        data,_=self.scan()
        self.assertEqual(data['items'][0]['roles'],['commenter'])

    def test_budget_failure_does_not_remove_saved_records(self):
        self.scan()
        with patch.object(c,'MAX_REQUESTS',1):
            data,errors=self.scan()
        self.assertFalse(errors)
        self.assertTrue(data['sync']['warnings'])
        self.assertEqual(len(data['items']),1)

    def test_paginated_evidence_and_250_commit_limit(self):
        client=Mock()
        client.get.side_effect=[[commit(str(i)) for i in range(100)],[commit('last')]]
        scanner=c.Scanner(app.db_conn,client,PROFILE,self.settings,'2026-01-01')
        self.assertEqual(len(scanner.pages('/fixture')),101)
        self.assertEqual(client.get.call_args_list[1].args[1]['page'],2)
        self.client.prs[1]['commits']=300
        def pages(route,params=None,limit=10):
            return [commit(str(i)) for i in range(250)] if route.endswith('/commits') else []
        scanner=c.Scanner(app.db_conn,self.client,PROFILE,self.settings,'2026-01-01')
        with patch.object(scanner,'pages',side_effect=pages):
            scanner.inspect_pr(REPO,1)
        self.assertIn('250-commit',scanner.warnings[0])

    def test_followed_pr_alerts_once_even_without_github_thread(self):
        data,_=self.scan();item=data['items'][0]
        with app.db_conn() as con:
            with patch.object(c,'utcnow',return_value='2026-11-09T00:00:00Z'):
                c.update(con,{'username':'sample-user','id':item['id'],'followed':True})
            con.execute('INSERT INTO notification_accounts VALUES (?,?,?)',('sample-user','2026-11-09T00:00:00Z',None))
        client=Mock(interval=60)
        client.get.side_effect=lambda route: (PROFILE,{}) if route=='/user' else (pr(),{})
        client.fetch.return_value=([],{})
        monitor=n.Monitor(app.db_conn,lambda:'test-token')
        with patch.object(n,'desktop_alert') as alert:
            self.assertEqual(monitor.poll(client)['new'],1)
            self.assertEqual(len(alert.call_args.args[0]),1)
        with app.db_conn() as con:
            con.execute('UPDATE notification_monitor SET next_check=NULL')
        self.assertEqual(monitor.poll(client)['new'],0)
        self.assertEqual(monitor.snapshot()['items'][0]['reason'],'contribution')

    def test_follow_failure_does_not_block_other_notifications(self):
        data,_=self.scan()
        with app.db_conn() as con:
            c.update(con,{'username':'sample-user','id':data['items'][0]['id'],'followed':True})
            con.execute('INSERT INTO notification_accounts VALUES (?,?,?)',('sample-user','2026-11-09T00:00:00Z',None))
        client=Mock(interval=60)
        def get(route):
            if route=='/user': return PROFILE,{}
            raise n.APIError('PR no longer accessible',300)
        client.get.side_effect=get
        client.fetch.return_value=([{'id':'42','updated_at':DATE,'unread':True,'reason':'mention','repository':{'full_name':REPO},'subject':{'title':'Another conversation','type':'Issue','url':f'https://api.github.com/repos/{REPO}/issues/99'}}],{})
        monitor=n.Monitor(app.db_conn,lambda:'test-token')
        with patch.object(n,'desktop_alert'):
            self.assertEqual(monitor.poll(client)['new'],1)
        self.assertEqual(monitor.snapshot()['unread'],1)
        self.assertIn('no longer accessible',monitor.snapshot()['status']['error'])

    def test_delayed_github_thread_does_not_repeat_follow_alert(self):
        data,_=self.scan()
        with app.db_conn() as con:
            with patch.object(c,'utcnow',return_value='2026-11-09T00:00:00Z'):
                c.update(con,{'username':'sample-user','id':data['items'][0]['id'],'followed':True})
            con.execute('INSERT INTO notification_accounts VALUES (?,?,?)',('sample-user','2026-11-09T00:00:00Z',None))
        client=Mock(interval=60)
        client.get.side_effect=lambda route:(PROFILE,{}) if route=='/user' else (pr(),{})
        client.fetch.return_value=([],{})
        monitor=n.Monitor(app.db_conn,lambda:'test-token')
        with patch.object(n,'desktop_alert'):
            self.assertEqual(monitor.poll(client)['new'],1)
            with app.db_conn() as con:con.execute('UPDATE notification_monitor SET next_check=NULL')
            client.fetch.return_value=([{'id':'42','updated_at':DATE,'unread':True,'reason':'mention','repository':{'full_name':REPO},'subject':{'title':'Shared feature','type':'PullRequest','url':f'https://api.github.com/repos/{REPO}/pulls/1','latest_comment_url':'https://api.github.com/comments/42'}}],{})
            self.assertEqual(monitor.poll(client)['new'],0)
        self.assertEqual(len(monitor.snapshot()['items']),1)

    def test_settings_validation(self):
        self.assertEqual(c.validate_settings({'contribution_repos':['Example/Repo','example/repo']})['contribution_repos'],['example/repo'])
        for payload in [{'contribution_repos':['../../etc']},{'contribution_repos':['../etc']},{'contribution_repos':['owner/..']},{'contribution_repos':['owner/repo?x']},{'contribution_emails':['bad"query@example.com']},{'contribution_emails':'one@example.com'}]:
            with self.assertRaises(ValueError):c.validate_settings(payload)

    def test_main_sync_includes_contributions(self):
        self.client.search=lambda query:([],True)
        result=app.sync_github(self.client)
        self.assertEqual(result['status'],'success')
        self.assertEqual(len(app.dashboard({})['contributions']['items']),1)


if __name__=='__main__':unittest.main()
