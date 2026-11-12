#!/usr/bin/env python3
"""GitHub Workdesk: local GitHub + manual task dashboard. No pip dependencies.

Run: python3 app.py
Sync: python3 app.py sync [--notify]
Daily macOS reminder: python3 app.py install-reminder --time 09:00
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone, date
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import uuid
import webbrowser
import notifications
import contributions
import sync_diagnostics
import local_backups
import phone_access

APP_NAME = 'GitHub Workdesk'
ROOT = Path(__file__).resolve().parent
DEFAULT_USERNAME = ''  # Empty: use authenticated GitHub account automatically
DEFAULT_ORGS = []  # Optional filter; empty means include all repositories
AGENT_LABEL = 'local.github-workdesk.daily-sync'
WATCHER_LABEL = 'local.github-workdesk.notifications'
SYNC_LOCK = threading.Lock()
THEMES = {'system', 'github-light', 'github-dark', 'linear-dark', 'catppuccin-mocha', 'nord', 'dracula'}
HOSTED_TOKEN_PROVIDER = None  # Set only by the authenticated cloud entry point.
SYNC_STATE = {'running': False, 'started_at': None, 'finished_at': None, 'error': None, 'message': None, 'warnings': []}


def default_data_dir() -> Path:
    override = os.environ.get('WORKDESK_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'GitHub Workdesk'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GitHub Workdesk'
    return Path.home() / '.local' / 'share' / 'github-workdesk'


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None


def db_conn():
    folder = default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(folder / 'workdesk.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with db_conn() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          source TEXT NOT NULL CHECK(source IN ('github','manual')),
          kind TEXT NOT NULL DEFAULT 'task',
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          repo TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          github_state TEXT NOT NULL DEFAULT '',
          local_status TEXT NOT NULL DEFAULT 'todo',
          role_author INTEGER NOT NULL DEFAULT 0,
          role_review INTEGER NOT NULL DEFAULT 0,
          role_assigned INTEGER NOT NULL DEFAULT 0,
          was_review_requested INTEGER NOT NULL DEFAULT 0,
          is_draft INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          closed_at TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          priority INTEGER NOT NULL DEFAULT 2,
          due_date TEXT,
          notes TEXT NOT NULL DEFAULT '',
          labels TEXT NOT NULL DEFAULT '[]',
          snoozed_until TEXT,
          bookmarked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at DESC);
        CREATE INDEX IF NOT EXISTS tasks_repo ON tasks(repo);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT,
          type TEXT NOT NULL,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_created ON events(created_at DESC);
        CREATE TABLE IF NOT EXISTS sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          added INTEGER NOT NULL DEFAULT 0,
          changed INTEGER NOT NULL DEFAULT 0,
          errors TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          open_prs INTEGER NOT NULL,
          reviews INTEGER NOT NULL,
          issues INTEGER NOT NULL,
          manual_open INTEGER NOT NULL,
          completed INTEGER NOT NULL
        );
        ''')
        sync_diagnostics.migrate(con, 'sync_runs')
        notifications.init_db(con)
        contributions.init_db(con)
        for key, value in {
            'github_username': DEFAULT_USERNAME,
            'focus_orgs': DEFAULT_ORGS,
            'lookback_days': 365,
            'theme': 'system',
            'theme_configured': False,
        }.items():
            con.execute('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)', (key, json.dumps(value)))


def get_settings(con):
    return {r['key']: json.loads(r['value']) for r in con.execute('SELECT key,value FROM settings')}


def event(con, task_id, kind, msg):
    con.execute('INSERT INTO events (task_id,type,message,created_at) VALUES (?,?,?,?)',
                (task_id, kind, msg, utcnow()))


def github_token():
    if HOSTED_TOKEN_PROVIDER is not None:
        return HOSTED_TOKEN_PROVIDER()
    for var in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(var):
            return os.environ[var].strip()
    if not shutil.which('gh'):
        raise RuntimeError('GitHub CLI (gh) is not installed. Install it with: brew install gh; then run: gh auth login')
    proc = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                          capture_output=True, text=True, timeout=25)
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError('GitHub authentication is missing. Run: gh auth login')
    return proc.stdout.strip()


NOTIFICATIONS = notifications.Monitor(db_conn, github_token)
BACKUPS = local_backups.Backups(db_conn, default_data_dir)


class GitHubAPIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(self, token):
        self.token = token
        self._detail_cache = {}

    def get(self, route, params=None):
        if params is None and route in self._detail_cache:
            return self._detail_cache[route]
        url = 'https://api.github.com' + route
        if params:
            url += '?' + urlencode(params)
        req = Request(url, headers={
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'github-workdesk/1.0',
        })
        try:
            with urlopen(req, timeout=35) as response:
                data = json.load(response)
                if params is None:
                    self._detail_cache[route] = data
                return data
        except HTTPError as exc:
            try:
                msg = json.loads(exc.read().decode()).get('message', '')
            except (ValueError, UnicodeDecodeError):
                msg = exc.reason
            if exc.code in (401, 403):
                raise GitHubAPIError(exc.code, f'GitHub API {exc.code}: {msg}. Check your token/SSO/permissions.') from exc
            raise GitHubAPIError(exc.code, f'GitHub API {exc.code}: {msg}') from exc
        except URLError as exc:
            raise RuntimeError(f'Cannot connect to GitHub: {exc.reason}') from exc

    def search(self, query):
        """Return items, complete flag; incomplete must never clear a live review assignment."""
        found = []
        total = 0
        complete = True
        for page in range(1, 11):  # GitHub search caps at 1000 results.
            response = self.get('/search/issues', {
                'q': query, 'per_page': 100, 'page': page,
                'sort': 'updated', 'order': 'desc',
            })
            if page == 1:
                total = response.get('total_count', 0)
            found.extend(response.get('items', []))
            if response.get('incomplete_results'):
                complete = False
            if len(found) >= total or not response.get('items'):
                break
        return found, bool(complete and len(found) >= total and total <= 1000)


def normalize_gh(item, role, client=None):
    full_repo = item.get('repository_url', '').split('/repos/')[-1]
    if not full_repo:
        parts = urlparse(item['html_url']).path.strip('/').split('/')
        full_repo = '/'.join(parts[:2])
    kind = 'pr' if 'pull_request' in item else 'issue'
    state = item.get('state', 'open')
    merged_at = (item.get('pull_request') or {}).get('merged_at')
    is_draft = item.get('draft')
    # Search/issues often omits the PR's draft flag and merged_at. Verify via
    # the dedicated PR endpoint instead of guessing the state.
    if kind == 'pr' and client and ((state == 'closed' and not merged_at) or is_draft is None):
        try:
            detail = client.get(f'/repos/{full_repo}/pulls/{item["number"]}')
            merged_at = detail.get('merged_at') or merged_at
            is_draft = detail.get('draft', is_draft)
        except Exception:
            if state == 'closed' and not merged_at:
                # We know it is closed, but cannot tell merged vs closed.
                state = 'closed_unknown'
    if merged_at:
        state = 'merged'
    return {
        'id': 'gh:' + full_repo + '#' + str(item['number']),
        'source': 'github', 'kind': kind,
        'title': item.get('title') or '(Untitled)',
        'description': (item.get('body') or '')[:12000],
        'repo': full_repo, 'url': item.get('html_url', ''),
        'github_state': state,
        'is_draft': int(bool(is_draft)),
        'created_at': item.get('created_at') or utcnow(),
        'updated_at': item.get('updated_at') or utcnow(),
        'closed_at': merged_at or item.get('closed_at'),
        'labels': json.dumps([l['name'] for l in item.get('labels', []) if isinstance(l, dict)]),
    }


def merge_gh_task(con, item, role, now):
    existing = con.execute('SELECT * FROM tasks WHERE id=?', (item['id'],)).fetchone()
    role_col = {'author': 'role_author', 'review': 'role_review', 'assigned': 'role_assigned'}[role]
    if not existing:
        con.execute('''INSERT INTO tasks
          (id,source,kind,title,description,repo,url,github_state,is_draft,created_at,updated_at,
           closed_at,first_seen_at,last_seen_at,labels,role_author,role_review,role_assigned,was_review_requested)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (item['id'], 'github', item['kind'], item['title'], item['description'], item['repo'],
           item['url'], item['github_state'], item['is_draft'], item['created_at'], item['updated_at'],
           item['closed_at'], now, now, item['labels'], int(role == 'author'),
           int(role == 'review'), int(role == 'assigned'), int(role == 'review')))
        event(con, item['id'], 'discovered', f'Added {item["repo"]} · {item["title"][:100]}')
        if role == 'review':
            event(con, item['id'], 'review_requested', 'Your review is requested')
        return 'new'
    updates = dict(item)
    updates.pop('id')
    updates.pop('source', None)
    updates['last_seen_at'] = now
    updates[role_col] = 1
    if role == 'review':
        updates['was_review_requested'] = 1
    if role == 'assigned':
        updates['local_status'] = 'todo'  # Reset locally archived unassignment when reassigned.
    changes = []
    for key in ('github_state', 'title', 'is_draft'):
        if existing[key] != updates[key]:
            changes.append(key)
    if role == 'review' and not existing['role_review']:
        event(con, item['id'], 'review_requested', 'Your review is requested')
    if 'github_state' in changes:
        event(con, item['id'], 'status_changed', f'GitHub status: {existing["github_state"] or "unknown"} → {updates["github_state"]}')
    if 'title' in changes or 'is_draft' in changes:
        event(con, item['id'], 'updated', 'GitHub PR/issue metadata changed')
    cols = ','.join(f'{key}=?' for key in updates)
    con.execute(f'UPDATE tasks SET {cols} WHERE id=?', (*updates.values(), item['id']))
    return 'updated' if changes or (role == 'review' and not existing['role_review']) else 'same'


def summary_counts(con):
    tasks = [dict(r) for r in con.execute('SELECT * FROM tasks')]
    open_state = lambda t: t['github_state'] not in ('closed', 'merged', 'closed_unknown')
    return {
        'open_prs': sum(t['source'] == 'github' and t['kind'] == 'pr' and t['role_author'] and open_state(t) for t in tasks),
        'reviews': sum(t['source'] == 'github' and t['role_review'] and open_state(t) for t in tasks),
        'issues': sum(t['source'] == 'github' and t['kind'] == 'issue' and t['role_assigned'] and open_state(t) for t in tasks),
        'manual_open': sum(t['source'] == 'manual' and t['local_status'] != 'done' for t in tasks),
        'completed': sum((t['source'] == 'manual' and t['local_status'] == 'done') or
                         (t['source'] == 'github' and t['github_state'] in ('merged', 'closed', 'closed_unknown')) or
                         (t['source'] == 'github' and not t['role_review'] and not t['role_author'] and not t['role_assigned'] and (t['was_review_requested'] or t['local_status'] == 'unassigned'))
                         for t in tasks),
    }


def sync_github(client=None):
    """Reconcile GitHub without ever deleting user notes, priorities or historical records."""
    if not SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError('A sync is already in progress.')
    lock_handle = None
    try:
        # Cross-process lock protects web sync vs. launchd sync.
        if os.name != 'nt':
            import fcntl
            lock_handle = open(default_data_dir() / 'sync.lock', 'a+')
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another Workdesk process is already syncing.')
        started = utcnow()
        SYNC_STATE.update(running=True, started_at=started, error=None, warnings=[], message='Syncing with GitHub')
        with db_conn() as con:
            opts = get_settings(con)
            cur = con.execute('INSERT INTO sync_runs(started_at,status) VALUES (?,?)', (started, 'running'))
            run_id = cur.lastrowid
        since = (date.today() - timedelta(days=int(opts['lookback_days']))).isoformat()
        client = client or GitHubClient(github_token())
        profile = client.get('/user')
        actual_user = str(profile.get('login', '')).strip()
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', actual_user):
            raise RuntimeError('Could not determine the authenticated GitHub account.')
        # Users can optionally pin an account, otherwise Workdesk follows gh auth login.
        username = str(opts.get('github_username') or '').strip() or actual_user
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', username):
            raise ValueError('Invalid GitHub username')
        if actual_user.lower() != username.lower():
            raise RuntimeError(f'Authenticated as {actual_user!r}, but Workdesk is configured for {username!r}. Change settings or gh account.')
        searches = [
            ('author', f'is:pr author:{username} is:open'),
            ('author', f'is:pr author:{username} is:closed updated:>={since}'),
            ('review', f'is:pr review-requested:{username} is:open'),
            ('assigned', f'is:issue assignee:{username} is:open'),
            ('author', f'is:issue author:{username} is:open'),
            ('author', f'is:issue author:{username} is:closed updated:>={since}'),
        ]
        counts = {'new': 0, 'updated': 0}
        errors = []
        review_ids = set()
        assigned_ids = set()
        review_complete = True
        assigned_complete = True
        now = utcnow()
        for role, query in searches:
            try:
                results, complete = client.search(query)
                if role == 'review':
                    review_complete = complete
                if role == 'assigned':
                    assigned_complete = complete
                with db_conn() as con:
                    for raw in results:
                        item = normalize_gh(raw, role, client)
                        outcome = merge_gh_task(con, item, role, now)
                        if outcome in counts:
                            counts[outcome] += 1
                        if role == 'review':
                            review_ids.add(item['id'])
                        if role == 'assigned':
                            assigned_ids.add(item['id'])
                if not complete:
                    errors.append(f'Search incomplete or >1,000 results; preserved prior {role} assignments: {query}')
            except Exception as exc:
                errors.append(f'{query}: {exc}')
                if role == 'review':
                    review_complete = False
                if role == 'assigned':
                    assigned_complete = False
        SYNC_STATE['message'] = 'Finding your contributions and checking commit evidence'
        contribution_report = contributions.sync(db_conn, client, profile, opts, since)
        errors.extend('Contributions: ' + error for error in contribution_report['errors'])
        warnings = contribution_report['warnings']
        with db_conn() as con:
            old = con.execute("SELECT * FROM tasks WHERE source='github' AND (role_review=1 OR role_assigned=1)").fetchall()
            for task in old:
                if task['role_review'] and review_complete and task['id'] not in review_ids:
                    con.execute('UPDATE tasks SET role_review=0, closed_at=COALESCE(closed_at, ?) WHERE id=?', (utcnow(), task['id']))
                    event(con, task['id'], 'review_removed', 'Review no longer requested (reviewed or request withdrawn)')
                if task['role_assigned'] and assigned_complete and task['id'] not in assigned_ids:
                    con.execute("UPDATE tasks SET role_assigned=0, local_status='unassigned', closed_at=COALESCE(closed_at, ?) WHERE id=?", (utcnow(), task['id']))
                    event(con, task['id'], 'assignment_removed', 'No longer assigned on GitHub')
            con.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', ('last_synced_user', json.dumps(username)))
            counts_now = summary_counts(con)
            con.execute('''INSERT INTO snapshots(created_at,open_prs,reviews,issues,manual_open,completed)
                           VALUES (?,?,?,?,?,?)''', (utcnow(), *counts_now.values()))
            status = sync_diagnostics.outcome(errors, warnings)
            con.execute('''UPDATE sync_runs SET finished_at=?,status=?,added=?,changed=?,errors=?,warnings=? WHERE id=?''',
                        (utcnow(), status, counts['new'], counts['updated'], json.dumps(errors), json.dumps(warnings), run_id))
        result = {'status': status, 'added': counts['new'], 'changed': counts['updated'],
                  'errors': errors, 'warnings': warnings, 'counts': counts_now}
        SYNC_STATE.update(running=False, finished_at=utcnow(), error='; '.join(errors) if errors else None,
                          warnings=warnings, message=f'{counts["new"]} new · {counts["updated"]} updated')
        return result
    except Exception as exc:
        SYNC_STATE.update(running=False, finished_at=utcnow(), error=str(exc), message='Sync failed')
        with db_conn() as con:
            con.execute("UPDATE sync_runs SET finished_at=?,status='failed',errors=? WHERE status='running'",
                        (utcnow(), json.dumps([str(exc)])))
        raise
    finally:
        if lock_handle:
            lock_handle.close()
        SYNC_LOCK.release()


def scheduled_notification(result=None, error=None):
    if sys.platform != 'darwin' or not shutil.which('osascript'):
        return
    if error:
        title, message = APP_NAME + ' · Sync failed', str(error)[:160]
    else:
        c = result['counts']
        title = APP_NAME + ' · Daily refresh'
        message = f'{c["reviews"]} reviews · {c["open_prs"]} open PRs · {c["issues"]} issues · {c["manual_open"]} tasks'
    # No shell interpolation. AppleScript safely encodes strings via json escapes.
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    try:
        subprocess.run(['osascript', '-e', script], capture_output=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        pass


def agent_path():
    return Path.home() / 'Library' / 'LaunchAgents' / f'{AGENT_LABEL}.plist'


def install_reminder(time_string):
    if sys.platform != 'darwin':
        raise RuntimeError('Automatic reminders are currently implemented for macOS only.')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_string):
        raise ValueError('Time must be HH:MM (24-hour format).')
    import plistlib
    h, m = map(int, time_string.split(':'))
    target = agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    log_dir = default_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    env = {}
    # Do not persist bearer tokens in a plist. gh auth login uses OS credential store.
    if os.environ.get('WORKDESK_DATA_DIR'):
        env['WORKDESK_DATA_DIR'] = str(log_dir)
    config = {
        'Label': AGENT_LABEL,
        'ProgramArguments': [sys.executable, str(ROOT / 'app.py'), 'sync', '--notify'],
        'StartCalendarInterval': {'Hour': h, 'Minute': m},
        'StandardOutPath': str(log_dir / 'daily-sync.log'),
        'StandardErrorPath': str(log_dir / 'daily-sync-error.log'),
        'WorkingDirectory': str(ROOT),
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', **env},
    }
    with target.open('wb') as fp:
        plistlib.dump(config, fp)
    target.chmod(0o600)
    uid = str(os.getuid())
    subprocess.run(['launchctl', 'bootout', f'gui/{uid}', str(target)], capture_output=True)
    proc = subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', str(target)], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f'LaunchAgent saved, but could not be loaded: {proc.stderr.strip()}')
    return time_string


def remove_reminder():
    path = agent_path()
    if path.exists():
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
        path.unlink()


def reminder_info():
    path = agent_path()
    if not path.exists():
        return {'installed': False, 'time': '09:00'}
    try:
        import plistlib
        with path.open('rb') as fp:
            contents = plistlib.load(fp)
        interval = contents['StartCalendarInterval']
        return {'installed': True, 'time': f'{interval["Hour"]:02d}:{interval["Minute"]:02d}'}
    except (OSError, KeyError, ValueError):
        return {'installed': True, 'time': '09:00'}


def watcher_path():
    return Path.home() / 'Library' / 'LaunchAgents' / f'{WATCHER_LABEL}.plist'


def install_watcher():
    if sys.platform != 'darwin':
        raise RuntimeError('Background notification scheduling is currently macOS only.')
    import plistlib
    target = watcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    folder = default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    config = {
        'Label': WATCHER_LABEL,
        'ProgramArguments': [sys.executable, str(ROOT / 'app.py'), 'check-notifications'],
        'StartInterval': 60, 'RunAtLoad': True,
        'WorkingDirectory': str(ROOT),
        'StandardOutPath': str(folder / 'notifications.log'),
        'StandardErrorPath': str(folder / 'notifications-error.log'),
        # Never save bearer tokens in the plist. The job uses gh's credential store.
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                                 'WORKDESK_DATA_DIR': str(folder)},
    }
    with target.open('wb') as fp:
        plistlib.dump(config, fp)
    target.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain, str(target)], capture_output=True, timeout=15)
    proc = subprocess.run(['launchctl', 'bootstrap', domain, str(target)], capture_output=True, text=True, timeout=15)
    if proc.returncode:
        target.unlink(missing_ok=True)
        raise RuntimeError(f'Could not load background notifications: {proc.stderr.strip()}')
    with db_conn() as con:
        con.execute("UPDATE settings SET value='true' WHERE key='notifications_enabled'")


def remove_watcher():
    if sys.platform != 'darwin':
        raise RuntimeError('Background notification scheduling is currently macOS only.')
    target = watcher_path()
    if target.exists():
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(target)], capture_output=True, timeout=15)
        target.unlink()


def validate_http_url(value):
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('Link must start with http:// or https://')
    return value[:2000]


def validate_date(value):
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Date must have format YYYY-MM-DD')


def task_to_json(row):
    task = dict(row)
    task['labels'] = json.loads(task['labels'])
    for key in ('role_author', 'role_review', 'role_assigned', 'was_review_requested', 'is_draft', 'bookmarked'):
        task[key] = bool(task[key])
    return task


def save_manual(payload, item_id=None):
    title = str(payload.get('title', '')).strip()
    if not title or len(title) > 300:
        raise ValueError('Title is required (max 300 characters).')
    state = payload.get('local_status', 'todo')
    if state not in ('todo', 'in_progress', 'blocked', 'done'):
        raise ValueError('Invalid status')
    priority = int(payload.get('priority', 2))
    if priority not in (1, 2, 3):
        raise ValueError('Priority must be 1, 2 or 3')
    now = utcnow()
    due = validate_date(payload.get('due_date'))
    with db_conn() as con:
        if item_id:
            old = con.execute("SELECT * FROM tasks WHERE id=? AND source='manual'", (item_id,)).fetchone()
            if not old:
                raise KeyError('Manual task not found')
            con.execute('''UPDATE tasks SET title=?, description=?, repo=?,url=?,local_status=?,priority=?,due_date=?,notes=?,updated_at=? WHERE id=?''',
                        (title, str(payload.get('description', ''))[:12000], str(payload.get('repo', ''))[:160],
                         validate_http_url(payload.get('url', '')), state, priority, due,
                         str(payload.get('notes', ''))[:10000], now, item_id))
            if old['local_status'] != state:
                con.execute('UPDATE tasks SET closed_at=? WHERE id=?', (now if state == 'done' else None, item_id))
                event(con, item_id, 'manual_status', f'Status: {old["local_status"]} → {state}')
            else:
                event(con, item_id, 'manual_updated', 'Manual task edited')
        else:
            item_id = 'manual:' + uuid.uuid4().hex
            con.execute('''INSERT INTO tasks (id,source,kind,title,description,repo,url,local_status,created_at,updated_at,first_seen_at,last_seen_at,priority,due_date,notes)
                           VALUES (?,'manual','task',?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (item_id, title, str(payload.get('description', ''))[:12000], str(payload.get('repo', ''))[:160],
                         validate_http_url(payload.get('url', '')), state, now, now, now, now,
                         priority, due, str(payload.get('notes', ''))[:10000]))
            if state == 'done':
                con.execute('UPDATE tasks SET closed_at=? WHERE id=?', (now, item_id))
            event(con, item_id, 'manual_created', f'Created task: {title[:100]}')
        return task_to_json(con.execute('SELECT * FROM tasks WHERE id=?', (item_id,)).fetchone())


def update_task(item_id, payload):
    with db_conn() as con:
        old = con.execute('SELECT * FROM tasks WHERE id=?', (item_id,)).fetchone()
        if not old:
            raise KeyError('Task not found')
        if old['source'] == 'manual' and 'title' in payload:
            return save_manual(payload, item_id)
        values = {}
        for field in ('priority', 'due_date', 'notes', 'snoozed_until', 'bookmarked'):
            if field in payload:
                v = payload[field]
                if field == 'priority':
                    v = int(v)
                    if v not in (1, 2, 3):
                        raise ValueError('Priority must be 1, 2 or 3')
                elif field == 'due_date':
                    v = validate_date(v)
                elif field == 'snoozed_until':
                    v = validate_date(v)
                elif field == 'bookmarked':
                    v = int(bool(v))
                elif field == 'notes':
                    v = str(v)[:10000]
                values[field] = v
        if old['source'] == 'manual' and 'local_status' in payload:
            if payload['local_status'] not in ('todo', 'in_progress', 'blocked', 'done'):
                raise ValueError('Invalid status')
            values['local_status'] = payload['local_status']
        if not values:
            raise ValueError('No supported changes provided')
        if old['source'] == 'manual':
            values['updated_at'] = utcnow()
            if 'local_status' in values and values['local_status'] != old['local_status']:
                values['closed_at'] = utcnow() if values['local_status'] == 'done' else None
        assignments = ', '.join(f'{name}=?' for name in values)
        con.execute(f'UPDATE tasks SET {assignments} WHERE id=?', (*values.values(), item_id))
        event(con, item_id, 'local_updated', 'Local task details updated')
        return task_to_json(con.execute('SELECT * FROM tasks WHERE id=?', (item_id,)).fetchone())


def dashboard(filters):
    with db_conn() as con:
        data = [task_to_json(r) for r in con.execute('SELECT * FROM tasks ORDER BY updated_at DESC')]
        settings = get_settings(con)
        repo_values = sorted(set(t['repo'] for t in data if t['repo']))
        events = [dict(x) for x in con.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 100')]
        runs = [dict(x) for x in con.execute('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 10')]
        for run in runs:
            run['errors'] = json.loads(run['errors'])
            run['warnings'] = json.loads(run['warnings'])
        snapshots = [dict(x) for x in con.execute('SELECT * FROM snapshots ORDER BY id DESC LIMIT 90')][::-1]
        return {'tasks': data, 'events': events, 'runs': runs, 'snapshots': snapshots,
                'repositories': repo_values, 'settings': settings, 'sync': SYNC_STATE.copy(),
                'summary': summary_counts(con), 'reminder': reminder_info(),
                'data_dir': str(default_data_dir()), 'platform': sys.platform,
                'notification_watcher': watcher_path().exists(), 'contributions': contributions.snapshot(con),
                'storage': BACKUPS.info() if HOSTED_TOKEN_PROVIDER is None else None}


class WorkdeskAPI:
    local_storage = False
    server_version = 'GitHub-Workdesk/1.0'

    def allowed_host(self):
        try:
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
        except ValueError:
            return False
        return host in ('127.0.0.1', 'localhost', '::1')

    def log_message(self, fmt, *args):
        if not self.path.startswith(('/api/dashboard', '/api/notifications')):
            super().log_message(fmt, *args)

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_binary(self, data, name, content_type):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, file_name, content_type):
        path = ROOT / 'static' / file_name
        data = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_error(self, exc):
        status = 404 if isinstance(exc, KeyError) else 400 if isinstance(exc, (ValueError, TypeError)) else 500
        return self.send_json({'error': str(exc)}, status)

    def do_GET(self):
        if not self.allowed_host():
            return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
        try:
            uri = urlparse(self.path)
            if uri.path == '/':
                return self.serve_static('index.html', 'text/html; charset=utf-8')
            if uri.path == '/static/sync-status.js':
                return self.serve_static('sync-status.js', 'text/javascript; charset=utf-8')
            if uri.path == '/static/local-data.js':
                return self.serve_static('local-data.js', 'text/javascript; charset=utf-8')
            if uri.path == '/static/contributions.js':
                return self.serve_static('contributions.js', 'text/javascript; charset=utf-8')
            if uri.path == '/static/app.js':
                return self.serve_static('app.js', 'text/javascript; charset=utf-8')
            if uri.path == '/static/styles.css':
                return self.serve_static('styles.css', 'text/css; charset=utf-8')
            mobile_assets = {'phone-access.js': 'text/javascript', 'mobile.js': 'text/javascript', 'mobile.css': 'text/css',
                             'icon-192.png': 'image/png', 'icon-512.png': 'image/png'}
            name = uri.path.removeprefix('/static/')
            if uri.path.startswith('/static/') and name in mobile_assets:
                return self.serve_static(name, mobile_assets[name])
            if uri.path == '/favicon.ico':
                self.send_response(204); self.end_headers(); return
            if uri.path == '/api/dashboard':
                return self.send_json(dashboard(parse_qs(uri.query)))
            if uri.path == '/api/notifications':
                return self.send_json(NOTIFICATIONS.snapshot())
            if uri.path == '/api/health':
                return self.send_json({'ok': True, 'timestamp': utcnow()})
            if uri.path == '/api/export':
                with db_conn() as con:
                    data = {name: [dict(r) for r in con.execute(f'SELECT * FROM {name}')]
                            for name in ('tasks', 'events', 'sync_runs', 'snapshots', 'settings', 'notifications', 'notification_accounts', 'notification_monitor', 'contributions', 'contribution_sync')}
                data['exported_at'] = utcnow()
                return self.send_binary(json.dumps(data, ensure_ascii=False, indent=2).encode(),
                                        'workdesk-export.json', 'application/json')
            if uri.path == '/api/backup':
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'backup.sqlite3'
                    with db_conn() as con:
                        with sqlite3.connect(path) as target:
                            con.backup(target)
                    return self.send_binary(path.read_bytes(), 'workdesk-backup.sqlite3', 'application/vnd.sqlite3')
            if uri.path == '/api/export.csv':
                with db_conn() as con:
                    rows = [dict(r) for r in con.execute('SELECT * FROM tasks')]
                f = io.StringIO()
                if rows:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                    writer.writeheader(); writer.writerows(rows)
                return self.send_binary(f.getvalue().encode('utf-8-sig'), 'workdesk-tasks.csv', 'text/csv; charset=utf-8')
            return self.send_json({'error': 'Not found'}, 404)
        except Exception as exc:
            return self.handle_error(exc)

    def read_payload(self):
        # Bind server to localhost only, and reject cross-origin browser POSTs.
        origin = self.headers.get('Origin', '')
        if origin and origin != f'http://{self.headers.get("Host", "")}':
            raise ValueError('Cross-origin request rejected')
        size = int(self.headers.get('Content-Length', '0'))
        if size > 256 * 1024:
            raise ValueError('Request body too large')
        return json.loads(self.rfile.read(size) or b'{}')

    def do_POST(self):
        if not self.allowed_host():
            return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
        try:
            uri = urlparse(self.path)
            p = self.read_payload()
            if uri.path == '/api/local-backup':
                if not self.local_storage:
                    return self.send_json({'error': 'Local backups are available only on your computer.'}, 404)
                return self.send_json(BACKUPS.create())
            if uri.path == '/api/open-data-folder':
                if not self.local_storage:
                    return self.send_json({'error': 'This action is available only on your computer.'}, 404)
                folder = default_data_dir()
                if sys.platform == 'darwin':
                    subprocess.run(['open', str(folder)], check=True, timeout=10)
                elif os.name == 'nt':
                    os.startfile(str(folder))
                elif shutil.which('xdg-open'):
                    subprocess.run(['xdg-open', str(folder)], check=True, timeout=10)
                else:
                    raise ValueError('Open the data folder shown in Settings using your file manager.')
                return self.send_json({'opened': True})
            if uri.path == '/api/sync':
                if SYNC_STATE['running']:
                    return self.send_json({'error': 'Sync already in progress'}, 409)
                def work():
                    try:
                        sync_github()
                    except Exception as exc:
                        print('GitHub sync failed:', exc, file=sys.stderr)
                threading.Thread(target=work, daemon=True).start()
                return self.send_json({'started': True}, 202)
            if uri.path == '/api/contributions':
                with db_conn() as con:
                    contributions.update(con, p)
                return self.send_json({'ok': True})
            if uri.path == '/api/notifications/read':
                NOTIFICATIONS.mark_read(p)
                return self.send_json({'ok': True})
            if uri.path == '/api/notification-watcher':
                if p.get('action') == 'install':
                    install_watcher()
                elif p.get('action') == 'remove':
                    remove_watcher()
                else:
                    raise ValueError('Unknown watcher action')
                return self.send_json({'installed': watcher_path().exists()})
            if uri.path == '/api/tasks':
                return self.send_json(save_manual(p), 201)
            if uri.path == '/api/settings':
                with db_conn() as con:
                    if 'theme' in p:
                        theme = p['theme']
                        if not isinstance(theme, str) or theme not in THEMES:
                            raise ValueError('Choose a supported appearance theme.')
                        con.execute("UPDATE settings SET value=? WHERE key='theme'", (json.dumps(theme),))
                        con.execute("UPDATE settings SET value='true' WHERE key='theme_configured'")
                    for key, value in contributions.validate_settings(p).items():
                        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))
                    for key in ('notifications_enabled', 'desktop_notifications'):
                        if key in p:
                            if not isinstance(p[key], bool):
                                raise ValueError(f'{key} must be true or false')
                            con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(p[key]), key))
                    if 'notification_interval' in p:
                        interval = p['notification_interval']
                        if type(interval) is not int or not 60 <= interval <= 3600:
                            raise ValueError('Notification interval must be 60–3600 seconds')
                        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(interval), 'notification_interval'))
                    if 'github_username' in p:
                        name = str(p['github_username']).strip()
                        if name and not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', name):
                            raise ValueError('Invalid GitHub username')
                        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(name), 'github_username'))
                    if 'focus_orgs' in p:
                        orgs = p['focus_orgs']
                        if not isinstance(orgs, list) or len(orgs) > 20 or any(not isinstance(v, str) or len(v) > 60 for v in orgs):
                            raise ValueError('Invalid organization list')
                        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(orgs), 'focus_orgs'))
                    if 'lookback_days' in p:
                        days = int(p['lookback_days'])
                        if days < 30 or days > 1825:
                            raise ValueError('History sync window must be 30–1825 days')
                        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(days), 'lookback_days'))
                return self.send_json({'ok': True})
            if uri.path == '/api/reminder':
                if p.get('action') == 'install':
                    return self.send_json({'installed': True, 'time': install_reminder(p.get('time', '09:00'))})
                if p.get('action') == 'remove':
                    remove_reminder()
                    return self.send_json({'installed': False})
                raise ValueError('Unknown reminder action')
            return self.send_json({'error': 'Not found'}, 404)
        except Exception as exc:
            return self.handle_error(exc)

    def do_PATCH(self):
        if not self.allowed_host():
            return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
        try:
            if not self.path.startswith('/api/tasks/'):
                return self.send_json({'error': 'Not found'}, 404)
            item_id = unquote(self.path[len('/api/tasks/'):])
            return self.send_json(update_task(item_id, self.read_payload()))
        except Exception as exc:
            return self.handle_error(exc)

    def do_DELETE(self):
        if not self.allowed_host():
            return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
        try:
            if not self.path.startswith('/api/tasks/'):
                return self.send_json({'error': 'Not found'}, 404)
            self.read_payload()
            item_id = unquote(self.path[len('/api/tasks/'):])
            with db_conn() as con:
                task = con.execute('SELECT * FROM tasks WHERE id=?', (item_id,)).fetchone()
                if not task or task['source'] != 'manual':
                    raise ValueError('Only manual tasks can be deleted')
                con.execute('DELETE FROM tasks WHERE id=?', (item_id,))
                event(con, item_id, 'manual_deleted', f'Deleted manual task: {task["title"][:100]}')
            return self.send_json({'deleted': True})
        except Exception as exc:
            return self.handle_error(exc)


class Handler(WorkdeskAPI, BaseHTTPRequestHandler):
    """Local HTTP transport. Hosted mode supplies its own authenticated transport."""
    local_storage = True

    def do_GET(self):
        if urlparse(self.path).path == '/api/phone-access':
            if not self.allowed_host():
                return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
            return self.send_json(PHONE_ACCESS.info())
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == '/api/phone-access':
            if not self.allowed_host():
                return self.send_json({'error': 'Only localhost requests are accepted'}, 403)
            try:
                return self.send_json(PHONE_ACCESS.change(self.read_payload().get('action')))
            except Exception as exc:
                return self.handle_error(exc)
        return super().do_POST()


PHONE_ACCESS = phone_access.Access(default_data_dir, WorkdeskAPI)


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument('command', nargs='?', default='serve', choices=['serve', 'sync', 'backup', 'install-reminder', 'remove-reminder', 'check-notifications', 'install-notifications', 'remove-notifications'])
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--time', default='09:00')
    ap.add_argument('--notify', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    init_db()
    if args.command == 'backup':
        try:
            result = BACKUPS.create()
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f'Could not save local backup: {exc}', file=sys.stderr)
            return 1
    if args.command in ('serve', 'sync', 'check-notifications'):
        BACKUPS.maybe_create()
    if args.command == 'check-notifications':
        result = NOTIFICATIONS.poll()
        print(json.dumps(result))
        return 1 if result['status'] == 'error' else 0
    if args.command == 'install-notifications':
        install_watcher()
        print('Background GitHub notifications enabled (macOS, while logged in).')
        return 0
    if args.command == 'remove-notifications':
        remove_watcher()
        print('Background GitHub notification watcher removed.')
        return 0
    if args.command == 'sync':
        try:
            result = sync_github()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.notify:
                scheduled_notification(result=result)
            return 0 if result['status'] in ('success', 'limited') else 2
        except Exception as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            if args.notify:
                scheduled_notification(error=exc)
            return 1
    if args.command == 'install-reminder':
        print(f'Daily reminder installed: {install_reminder(args.time)} local time')
        return 0
    if args.command == 'remove-reminder':
        remove_reminder(); print('Daily reminder removed'); return 0
    if not 1024 <= args.port <= 65535:
        ap.error('--port must be between 1024 and 65535')
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    PHONE_ACCESS.start()
    url = f'http://127.0.0.1:{args.port}'
    print(f'\n  GitHub Workdesk  {url}\n  Database: {default_data_dir() / "workdesk.sqlite3"}\n  Ctrl+C to stop\n', flush=True)
    threading.Thread(target=NOTIFICATIONS.run, daemon=True).start()
    threading.Thread(target=BACKUPS.run, args=(NOTIFICATIONS.stop,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        NOTIFICATIONS.stop.set()
        PHONE_ACCESS.close()
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
