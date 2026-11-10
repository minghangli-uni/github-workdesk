"""Account-scoped contribution discovery, evidence and local annotations."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re

REPO = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+\Z')
EMAIL = re.compile(r'[^\s<>"\\]+@[^\s<>"\\]+\.[^\s<>"\\]+\Z')
MAX_REQUESTS = 500
MAX_REPOS = 30
MAX_PRS = 200


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def init_db(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS contributions (
            username TEXT NOT NULL, id TEXT NOT NULL, repo TEXT NOT NULL,
            kind TEXT NOT NULL, data TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
            followed INTEGER NOT NULL DEFAULT 0, follow_updated_at TEXT,
            PRIMARY KEY(username,id)
        );
        CREATE TABLE IF NOT EXISTS contribution_sync (
            username TEXT PRIMARY KEY, checked_at TEXT NOT NULL, errors TEXT NOT NULL,
            requests INTEGER NOT NULL, repositories INTEGER NOT NULL
        );
    ''')
    for key in ('contribution_repos', 'contribution_emails'):
        con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)', (key, '[]'))


def validate_settings(payload):
    result = {}
    for key, pattern, limit in (('contribution_repos', REPO, 20), ('contribution_emails', EMAIL, 10)):
        if key not in payload:
            continue
        values = payload[key]
        if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or len(v) > 254 or not pattern.fullmatch(v) for v in values):
            raise ValueError(f'Invalid {key.replace("_", " ")}; supply up to {limit} valid values.')
        result[key] = list(dict.fromkeys(v.lower() for v in values))
    return result


def account(con):
    settings = {r['key']: json.loads(r['value']) for r in con.execute('SELECT key,value FROM settings')}
    return str(settings.get('github_username') or settings.get('last_synced_user') or '').lower()


def snapshot(con):
    username = account(con)
    items = []
    for row in con.execute('SELECT * FROM contributions WHERE username=?', (username,)):
        data = json.loads(row['data'])
        # A commit with a known PR is displayed under that PR's evidence.
        if data.get('linked_prs') and not row['notes']:
            continue
        items.append({**data, 'notes': row['notes'], 'followed': bool(row['followed'])})
    run = con.execute('SELECT * FROM contribution_sync WHERE username=?', (username,)).fetchone()
    status = dict(run) if run else None
    if status:
        status['errors'] = json.loads(status['errors'])
    return {'username': username, 'items': items, 'sync': status}


def update(con, payload):
    username = account(con)
    if payload.get('username') != username:
        raise ValueError('GitHub account changed. Refresh your contributions.')
    row = con.execute('SELECT * FROM contributions WHERE username=? AND id=?', (username, payload.get('id'))).fetchone()
    if not row:
        raise KeyError('Contribution not found')
    if 'notes' in payload:
        if not isinstance(payload['notes'], str) or len(payload['notes']) > 10000:
            raise ValueError('Notes must be text, up to 10,000 characters.')
        con.execute('UPDATE contributions SET notes=? WHERE username=? AND id=?', (payload['notes'], username, row['id']))
    if 'followed' in payload:
        if type(payload['followed']) is not bool or row['kind'] != 'pr':
            raise ValueError('Only pull requests can be followed.')
        count = con.execute('SELECT count(*) FROM contributions WHERE username=? AND followed=1', (username,)).fetchone()[0]
        if payload['followed'] and not row['followed'] and count >= 50:
            raise ValueError('Up to 50 pull requests can be followed at once.')
        baseline = utcnow() if payload['followed'] and not row['followed'] else row['follow_updated_at']
        con.execute('UPDATE contributions SET followed=?,follow_updated_at=? WHERE username=? AND id=?', (int(payload['followed']), baseline, username, row['id']))


def followed_updates(db_conn, client, username):
    """Fetch followed PR changes; caller commits checkpoints with its notification inbox."""
    with db_conn() as con:
        rows = con.execute('SELECT * FROM contributions WHERE username=? AND followed=1', (username,)).fetchall()
    updates, errors, delay, routes = [], [], 0, {}
    for row in rows:
        old = json.loads(row['data'])
        routes[old['url']] = 'workdesk:' + row['id']
        try:
            pr, _ = client.get(f'/repos/{row["repo"]}/pulls/{old["number"]}')
        except Exception as exc:
            errors.append(f'Followed PR {row["repo"]}#{old["number"]}: {exc}')
            delay = max(delay, getattr(exc, 'delay', 0))
            continue
        updated = pr.get('updated_at', '')
        if updated > (row['follow_updated_at'] or old['updated_at']):
            updates.append((row['id'], updated, {
                'id': 'workdesk:' + row['id'], 'updated_at': updated, 'unread': True,
                'reason': 'contribution', 'repository': {'full_name': row['repo']},
                'subject': {'title': pr['title'], 'type': 'PullRequest',
                            'url': f'https://api.github.com/repos/{row["repo"]}/pulls/{old["number"]}'},
            }))
    return updates, errors, delay, routes


def identities(profile, extra=()):
    login = profile['login'].lower()
    emails = {v.lower() for v in extra if EMAIL.fullmatch(v)}
    if profile.get('email'):
        emails.add(profile['email'].lower())
    emails.add(login + '@users.noreply.github.com')
    if profile.get('id'):
        emails.add(f'{profile["id"]}+{login}@users.noreply.github.com')
    return emails


def commit_evidence(raw, repo, username, emails):
    git = raw.get('commit') or {}
    message = git.get('message') or ''
    roles = []
    for field, role in (('author', 'code'), ('committer', 'committer')):
        actor = raw.get(field) or {}
        identity = git.get(field) or {}
        if (actor.get('login') or '').lower() == username or (identity.get('email') or '').lower() in emails:
            roles.append(role)
    normalized = message.replace('\r\n', '\n').strip()
    trailer_block = normalized.rsplit('\n\n', 1)[-1] if '\n\n' in normalized else ''
    trailers = re.findall(r'^Co-authored-by:[ \t]*[^\r\n<>]+<([^<>\r\n]+)>[ \t]*$', trailer_block, re.I | re.M)
    if any(email.strip().lower() in emails for email in trailers):
        roles.append('coauthor')
    if not roles:
        return None
    sha = raw['sha']
    author_date = (git.get('author') or {}).get('date')
    commit_date = (git.get('committer') or {}).get('date')
    contribution_date = author_date if 'code' in roles or 'coauthor' in roles else commit_date
    return {'id': 'commit:' + sha, 'type': 'commit', 'sha': sha, 'roles': roles,
            'title': (message.splitlines() or ['Commit'])[0][:500],
            'url': f'https://github.com/{repo}/commit/{sha}',
            'date': contribution_date or author_date or commit_date or utcnow(),
            'activity_at': commit_date or author_date or utcnow()}


class ScanLimit(RuntimeError):
    pass


class Scanner:
    def __init__(self, db_conn, client, profile, settings, since):
        self.db_conn, self.client = db_conn, client
        self.username = profile['login'].lower()
        self.settings, self.since = settings, since
        self.emails = identities(profile, settings.get('contribution_emails', []))
        self.requests = 0
        self.errors = []
        self.repos = set(settings.get('contribution_repos', []))
        self.candidates = {}
        self.commits = {}
        self.cache = {}

    def get(self, route, params=None):
        key = (route, json.dumps(params, sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        if self.requests >= MAX_REQUESTS:
            raise ScanLimit('Contribution scan reached its request limit; saved results are retained. Narrow extra repositories or the history window.')
        self.requests += 1
        try:
            data = self.client.get(route, params)
        except RuntimeError as exc:
            if 'rate limit' in str(exc).lower():
                raise ScanLimit('GitHub rate-limited contribution discovery. Saved results are retained; sync again later.') from exc
            raise
        self.cache[key] = data
        return data

    def pages(self, route, params=None, limit=10):
        found = []
        for page in range(1, limit + 1):
            rows = self.get(route, {**(params or {}), 'per_page': 100, 'page': page})
            if not isinstance(rows, list):
                raise ValueError('Unexpected GitHub list response')
            found.extend(rows)
            if len(rows) < 100:
                return found
        raise ScanLimit(f'Incomplete contribution list: {route}. Previous evidence was retained.')

    def search(self, kind, query):
        found = []
        for page in range(1, 11):
            data = self.get('/search/' + kind, {'q': query, 'sort': 'updated' if kind == 'issues' else 'committer-date', 'order': 'desc', 'per_page': 100, 'page': page})
            rows = data.get('items', [])
            found.extend(rows)
            if data.get('incomplete_results'):
                self.errors.append('GitHub returned incomplete contribution search results.')
            if len(found) >= data.get('total_count', 0) or not rows:
                return found
        self.errors.append('A contribution search exceeded GitHub’s 1,000-result limit.')
        return found

    def candidate(self, repo, number):
        if not REPO.fullmatch(repo):
            return
        self.repos.add(repo.lower())
        self.candidates[f'pr:{repo.lower()}#{int(number)}'] = (repo.lower(), int(number))

    def save(self, item):
        with self.db_conn() as con:
            con.execute('''INSERT INTO contributions(username,id,repo,kind,data) VALUES (?,?,?,?,?)
                ON CONFLICT(username,id) DO UPDATE SET repo=excluded.repo,kind=excluded.kind,data=excluded.data''',
                (self.username, item['id'], item['repo'], item['kind'], json.dumps(item)))

    def discover(self):
        # Optional email permission improves attribution; no expanded auth is requested.
        try:
            for email in self.pages('/user/emails', limit=1):
                if email.get('verified') and EMAIL.fullmatch(email.get('email', '')):
                    self.emails.add(email['email'].lower())
        except RuntimeError:
            self.errors.append('Verified email lookup unavailable; co-author matching uses public, noreply and configured emails.')
        queries = [f'is:pr author:{self.username} is:open',
                   f'is:pr author:{self.username} is:closed updated:>={self.since}',
                   f'is:pr reviewed-by:{self.username} is:open',
                   f'is:pr reviewed-by:{self.username} is:closed updated:>={self.since}',
                   f'is:pr commenter:{self.username} is:open',
                   f'is:pr commenter:{self.username} is:closed updated:>={self.since}']
        for query in queries:
            try:
                for pr in self.search('issues', query):
                    repo = pr.get('repository_url', '').split('/repos/')[-1]
                    self.candidate(repo, pr['number'])
            except ScanLimit:
                raise
            except Exception as exc:
                self.errors.append(f'PR contribution discovery: {exc}')
        # Revisit previously discovered open PRs even if search no longer finds them.
        with self.db_conn() as con:
            for row in con.execute('SELECT data FROM contributions WHERE username=? AND kind="pr"', (self.username,)):
                data = json.loads(row['data'])
                if data['state'] == 'open':
                    self.candidate(data['repo'], data['number'])
            for row in con.execute("SELECT repo,url FROM tasks WHERE source='github' AND kind='pr'"):
                match = re.search(r'/pull/(\d+)$', row['url'])
                if match:
                    self.candidate(row['repo'], match[1])
        # Include repositories the user owns or directly collaborates on.
        try:
            repositories = self.get('/user/repos', {'affiliation':'owner,collaborator', 'sort':'pushed', 'per_page':100, 'page':1})
            for repo in repositories:
                if not repo.get('archived') and not repo.get('fork') and repo.get('full_name'):
                    self.repos.add(repo['full_name'].lower())
            if len(repositories) == 100:
                self.errors.append('Repository discovery was limited to 100 recently pushed repositories. Add missing repositories in Settings.')
        except ScanLimit:
            raise
        except Exception as exc:
            self.errors.append(f'Repository discovery: {exc}')
        queries = [f'author:{self.username}', f'committer:{self.username}']
        queries += [f'"Co-authored-by" "{email}"' for email in sorted(self.emails)[:15]]
        if len(self.emails) > 15:
            self.errors.append('Co-author search was limited to 15 email identities.')
        for query in queries:
            try:
                for raw in self.search('commits', f'{query} committer-date:>={self.since}'):
                    repo = (raw.get('repository') or {}).get('full_name', '').lower()
                    evidence = commit_evidence(raw, repo, self.username, self.emails)
                    if evidence and REPO.fullmatch(repo):
                        self.commits[(repo, raw['sha'])] = evidence
                        self.repos.add(repo)
            except ScanLimit:
                raise
            except Exception as exc:
                self.errors.append(f'Commit contribution discovery: {exc}')

    def inspect_pr(self, repo, number, extra_evidence=()):
        pr = self.get(f'/repos/{repo}/pulls/{number}')
        item_id = f'pr:{repo}#{number}'
        signature = hashlib.sha256(json.dumps([1, sorted(self.emails)]).encode()).hexdigest()
        with self.db_conn() as con:
            old = con.execute('SELECT data FROM contributions WHERE username=? AND id=?', (self.username, item_id)).fetchone()
        previous = json.loads(old['data']) if old else None
        if previous and previous.get('updated_at') == pr['updated_at'] and previous.get('identity_signature') == signature and previous.get('details_complete') and all(any(old['id'] == e['id'] for old in previous['evidence']) for e in extra_evidence):
            return previous
        evidence = list(extra_evidence)
        if (pr.get('user') or {}).get('login', '').lower() == self.username:
            evidence.append({'id': 'author', 'type':'author', 'roles':['author'], 'title':'Opened this pull request',
                             'url':f'https://github.com/{repo}/pull/{number}', 'date':pr['created_at']})
        complete = True
        try:
            commits = self.pages(f'/repos/{repo}/pulls/{number}/commits', limit=3)
            if len(commits) >= 250 and pr.get('commits', len(commits)) > len(commits):
                complete = False
                self.errors.append(f'PR {repo}#{number} exceeds GitHub’s 250-commit PR limit; available commit evidence was saved.')
            for raw in commits:
                item = commit_evidence(raw, repo, self.username, self.emails)
                if item:
                    evidence.append(item)
        except ScanLimit as exc:
            if self.requests >= MAX_REQUESTS:
                raise
            complete = False
            self.errors.append(str(exc))
        except Exception as exc:
            complete = False
            self.errors.append(f'Commit evidence for {repo}#{number}: {exc}')
        reviews = []
        try:
            reviews = self.pages(f'/repos/{repo}/pulls/{number}/reviews')
            for review in reviews:
                if (review.get('user') or {}).get('login', '').lower() == self.username and review.get('submitted_at') and review.get('state') != 'PENDING':
                    evidence.append({'id':f'review:{review["id"]}', 'type':'review', 'roles':['reviewer'],
                                     'title': 'Review: ' + review['state'].replace('_',' ').lower(),
                                     'date':review['submitted_at'], 'url':f'https://github.com/{repo}/pull/{number}#pullrequestreview-{review["id"]}'})
        except ScanLimit as exc:
            if self.requests >= MAX_REQUESTS:
                raise
            complete = False; self.errors.append(str(exc))
        except Exception as exc:
            complete = False; self.errors.append(f'Review evidence for {repo}#{number}: {exc}')
        # Discussion participation is opt-in in the UI; an assignment/mention alone is not evidence.
        try:
            for comment in self.pages(f'/repos/{repo}/issues/{number}/comments'):
                if (comment.get('user') or {}).get('login', '').lower() == self.username:
                    evidence.append({'id':f'comment:{comment["id"]}', 'type':'comment', 'roles':['commenter'],
                                     'title':'Commented on the conversation', 'date':comment['created_at'],
                                     'url':f'https://github.com/{repo}/pull/{number}#issuecomment-{comment["id"]}'})
        except ScanLimit as exc:
            if self.requests >= MAX_REQUESTS:
                raise
            complete = False; self.errors.append(str(exc))
        except Exception as exc:
            complete = False; self.errors.append(f'Comment evidence for {repo}#{number}: {exc}')
        if previous:
            # Keep the record of work even after force-pushes or removed reviews.
            evidence = [{**e, 'historical': complete} for e in previous['evidence']] + evidence
        evidence = list({e['id']: e for e in evidence}.values())
        if not evidence:
            return None
        state = 'merged' if pr.get('merged_at') else pr['state']
        status = state.title()
        if state == 'open':
            decisions = {}
            for review in sorted(reviews, key=lambda r: r.get('submitted_at') or ''):
                if review.get('state') in ('APPROVED','CHANGES_REQUESTED','DISMISSED'):
                    decisions[(review.get('user') or {}).get('login')] = review
            status = 'Changes requested' if any(r['state']=='CHANGES_REQUESTED' for r in decisions.values()) else 'Reviewed' if any(r['state']=='APPROVED' and r.get('commit_id')==(pr.get('head') or {}).get('sha') for r in decisions.values()) else 'Awaiting review'
            if not complete:
                status = 'Open · details incomplete'
            if pr.get('draft'):
                status = 'Draft'
        return {'id':item_id, 'kind':'pr', 'repo':repo, 'number':number, 'title':pr['title'],
                'url':f'https://github.com/{repo}/pull/{number}', 'state':state, 'status':status,
                'updated_at':pr['updated_at'], 'last_contributed_at':max(e['date'] for e in evidence),
                'roles':sorted({role for e in evidence for role in e['roles']}), 'evidence':evidence,
                'details_complete':complete, 'identity_signature':signature}

    def run(self):
        inspected = set()
        associations = {}
        try:
            self.discover()
            # Discover open branch contributions before classifying default-branch commits.
            extra = self.settings.get('contribution_repos', [])
            repositories = list(dict.fromkeys(extra + sorted(self.repos)))
            if len(repositories) > MAX_REPOS:
                self.errors.append(f'Open-PR scans limited to {MAX_REPOS} repositories. Extra repositories in Settings take priority.')
            for repo in repositories[:MAX_REPOS]:
                try:
                    for pr in self.pages(f'/repos/{repo}/pulls', {'state':'open','sort':'updated','direction':'desc'}, limit=2):
                        self.candidate(repo, pr['number'])
                except ScanLimit as exc:
                    if self.requests >= MAX_REQUESTS: raise
                    self.errors.append(str(exc))
                except Exception as exc:
                    self.errors.append(f'Open PR discovery for {repo}: {exc}')
            # Reserve most requests for PR evidence rather than commit-to-PR lookups.
            association_limit = min(100, max(0, (MAX_REQUESTS - self.requests) // 3))
            if len(self.commits) > association_limit:
                self.errors.append(f'PR association checks limited to {association_limit} commits this sync. Other commits are shown with unverified PR links.')
            for repo, sha in list(self.commits)[:association_limit]:
                try:
                    prs = self.pages(f'/repos/{repo}/commits/{sha}/pulls', limit=2)
                    associations[(repo,sha)] = [f'pr:{repo}#{pr["number"]}' for pr in prs]
                    for pr in prs:
                        self.candidate(repo, pr['number'])
                except ScanLimit as exc:
                    if self.requests >= MAX_REQUESTS: raise
                    self.errors.append(str(exc))
                except Exception as exc:
                    self.errors.append(f'PR association for {repo}@{sha[:7]}: {exc}')
            if len(self.candidates) > MAX_PRS:
                self.errors.append(f'Contribution scan limited to {MAX_PRS} PRs. Narrow repositories or the history window.')
            for repo, number in list(self.candidates.values())[:MAX_PRS]:
                try:
                    item_id = f'pr:{repo}#{number}'
                    extra = [self.commits[key] for key, ids in associations.items() if item_id in ids]
                    item = self.inspect_pr(repo, number, extra)
                    if item:
                        self.save(item)
                        inspected.add(item['id'])
                except ScanLimit:
                    raise
                except Exception as exc:
                    self.errors.append(f'PR contribution {repo}#{number}: {exc}')
        except Exception as exc:
            self.errors.append(str(exc))
        # Preserve discovered commits even when enrichment fails or a request budget is reached.
        for (repo, sha), evidence in self.commits.items():
            linked = [pid for pid in associations.get((repo,sha),[]) if pid in inspected]
            item = {'id':f'commit:{repo}@{sha}', 'kind':'commit', 'repo':repo, 'title':evidence['title'],
                    'url':evidence['url'], 'state':'committed', 'status':'Committed' if (repo,sha) in associations else 'Committed · PR link unverified',
                    'updated_at':evidence['activity_at'], 'last_contributed_at':evidence['date'],
                    'roles':evidence['roles'], 'evidence':[evidence], 'linked_prs':linked, 'details_complete':(repo,sha) in associations}
            self.save(item)
        errors = list(dict.fromkeys(self.errors))
        with self.db_conn() as con:
            con.execute('INSERT OR REPLACE INTO contribution_sync VALUES (?,?,?,?,?)',
                        (self.username, utcnow(), json.dumps(errors), self.requests, len(self.repos)))
        return errors


def sync(db_conn, client, profile, settings, since):
    return Scanner(db_conn, client, profile, settings, since).run()
