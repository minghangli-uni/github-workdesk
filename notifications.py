"""Read-only GitHub notification polling and a durable, local inbox."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import re


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def init_db(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS notification_accounts (
            username TEXT PRIMARY KEY, cursor TEXT, last_modified TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_monitor (
            id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL DEFAULT '',
            checked_at TEXT, next_check TEXT, error TEXT, failures INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO notification_monitor(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS notifications (
            username TEXT NOT NULL, thread_id TEXT NOT NULL, title TEXT NOT NULL,
            repo TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, url TEXT NOT NULL,
            updated_at TEXT NOT NULL, revision TEXT NOT NULL, unread INTEGER NOT NULL,
            alert_seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(username, thread_id)
        );
        CREATE INDEX IF NOT EXISTS notifications_updated ON notifications(updated_at DESC);
    ''')
    for key, value in {'notifications_enabled': True, 'notification_interval': 60,
                       'desktop_notifications': True}.items():
        con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)', (key, json.dumps(value)))


class APIError(RuntimeError):
    def __init__(self, message, delay=300):
        super().__init__(message)
        self.delay = delay


class Client:
    def __init__(self, token):
        self.token = token
        self.interval = 60

    def get(self, route, params=None, modified=None):
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'github-workdesk/1.0'}
        if modified:
            headers['If-Modified-Since'] = modified
        req = Request('https://api.github.com' + route + ('?' + urlencode(params) if params else ''), headers=headers)
        try:
            with urlopen(req, timeout=35) as response:
                self.interval = max(self.interval, int(response.headers.get('X-Poll-Interval', 60)))
                return json.load(response), response.headers
        except HTTPError as exc:
            self.interval = max(self.interval, int(exc.headers.get('X-Poll-Interval', 60)))
            if exc.code == 304:
                return None, exc.headers
            delay = self.interval
            retry = exc.headers.get('Retry-After', '')
            if retry:
                try:
                    delay = max(delay, int(retry))
                except ValueError:
                    try:
                        delay = max(delay, int((parsedate_to_datetime(retry) - now()).total_seconds()))
                    except (ValueError, TypeError):
                        pass
            if exc.headers.get('X-RateLimit-Remaining') == '0':
                delay = max(delay, int(exc.headers.get('X-RateLimit-Reset', 0)) - int(now().timestamp()) + 1)
            hint = (' Check gh auth login, token scopes and organization SSO. Notifications require '
                    'notifications or repo scope; fine-grained tokens are not supported.' if exc.code in (401, 403, 404) else '')
            raise APIError(f'GitHub notifications API {exc.code}.{hint}', max(300, delay)) from exc
        except URLError as exc:
            raise APIError(f'Cannot connect to GitHub notifications: {exc.reason}') from exc

    def fetch(self, account):
        items = []
        first_headers = {}
        # Overlap the watermark so updates at the boundary are never lost.
        since = stamp(datetime.fromisoformat(account['cursor'].replace('Z', '+00:00')) - timedelta(seconds=60)) if account else None
        for page in range(1, 101):
            params = {'participating': 'true', 'all': 'true' if account else 'false', 'per_page': 50, 'page': page}
            if since:
                params['since'] = since
            data, headers = self.get('/notifications', params, account['last_modified'] if account and page == 1 else None)
            if page == 1:
                first_headers = headers
            if data is None:
                return None, first_headers
            if not isinstance(data, list):
                raise APIError('Unexpected GitHub notifications response.')
            items.extend(data)
            if not any('rel="next"' in part for part in headers.get('Link', '').split(',')):
                return items, first_headers
        raise APIError('Notification history is too large to read in one check; the previous checkpoint was preserved.')


def desktop_alert(items):
    if not items or sys.platform != 'darwin' or not shutil.which('osascript'):
        return
    title = 'GitHub Workdesk'
    message = (f"{items[0]['repo']} · {items[0]['title']}" if len(items) == 1
               else f'{len(items)} new GitHub updates. Open your Workdesk notification inbox.')
    # Pass untrusted text as argv, never as executable AppleScript source.
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        subprocess.run(['osascript', '-e', script, title, message[:240]], capture_output=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        pass  # The durable inbox remains available if OS delivery is unavailable.


def normalize(raw):
    subject = raw.get('subject') or {}
    updated = raw['updated_at']
    datetime.fromisoformat(updated.replace('Z', '+00:00'))
    url = 'https://github.com/notifications'
    match = re.fullmatch(r'https://api\.github\.com/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pulls|discussions)/(\d+)', subject.get('url') or '')
    if match:
        repo, kind, number = match.groups()
        url = f'https://github.com/{repo}/{"pull" if kind == "pulls" else kind}/{number}'
    revision = hashlib.sha256(json.dumps([updated, subject.get('latest_comment_url'), raw.get('reason'), subject.get('title')]).encode()).hexdigest()
    return {'thread_id': str(raw['id']), 'title': str(subject.get('title') or 'GitHub update')[:1000],
            'repo': str((raw.get('repository') or {}).get('full_name') or ''),
            'kind': str(subject.get('type') or 'Thread'), 'reason': str(raw.get('reason') or 'activity'),
            'url': url, 'updated_at': updated, 'revision': revision, 'unread': int(bool(raw.get('unread')))}


class Monitor:
    def __init__(self, db_conn, token_provider):
        self.db_conn = db_conn
        self.token_provider = token_provider
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def settings(self, con):
        return {r['key']: json.loads(r['value']) for r in con.execute('SELECT key,value FROM settings')}

    def poll(self, client=None):
        if not self.lock.acquire(blocking=False):
            return {'status': 'busy'}
        claimed = False
        lock_file = None
        try:
            with self.db_conn() as con:
                # The OS releases this lock on a crash; the persisted due time also
                # prevents back-to-back requests from the server and scheduled job.
                lock_file = open(con.execute('PRAGMA database_list').fetchone()['file'] + '.notifications.lock', 'a+b')
                try:
                    if os.name == 'nt':
                        import msvcrt
                        if lock_file.tell() == 0:
                            lock_file.write(b'0')
                            lock_file.flush()
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return {'status': 'busy'}
                con.execute('BEGIN IMMEDIATE')
                opts = self.settings(con)
                status = dict(con.execute('SELECT * FROM notification_monitor WHERE id=1').fetchone())
                if not opts['notifications_enabled']:
                    return {'status': 'disabled'}
                if status['next_check'] and status['next_check'] > stamp(now()):
                    return {'status': 'waiting'}
                con.execute('UPDATE notification_monitor SET next_check=? WHERE id=1',
                            (stamp(now() + timedelta(seconds=opts['notification_interval'])),))
                claimed = True
            client = client or Client(self.token_provider())
            user, _ = client.get('/user')
            username = str(user.get('login', '')).lower()
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?', username):
                raise APIError('Could not determine the authenticated GitHub account.')
            pinned = opts.get('github_username', '').lower()
            if pinned and pinned != username:
                raise APIError(f'Authenticated as {username}, but Workdesk is configured for {pinned}.')
            with self.db_conn() as con:
                account = con.execute('SELECT * FROM notification_accounts WHERE username=?', (username,)).fetchone()
            started = now()
            raw, headers = client.fetch(account)
            # GitHub server time avoids skipping events when the local clock is ahead.
            watermark = stamp(parsedate_to_datetime(headers['Date'])) if headers.get('Date') else stamp(started)
            items = [normalize(item) for item in raw] if raw is not None else []
            alerts = []
            with self.db_conn() as con:
                con.execute('BEGIN IMMEDIATE')
                seq = con.execute('SELECT COALESCE(MAX(alert_seq),0) FROM notifications').fetchone()[0]
                for item in items:
                    old = con.execute('SELECT * FROM notifications WHERE username=? AND thread_id=?', (username, item['thread_id'])).fetchone()
                    if old and (old['revision'] == item['revision'] or old['updated_at'] > item['updated_at']):
                        continue
                    # First run imports the existing inbox quietly; later updates re-open a thread.
                    if account:
                        seq += 1
                        item['unread'] = 1
                        alerts.append(item)
                    con.execute('''INSERT OR REPLACE INTO notifications
                        (username,thread_id,title,repo,kind,reason,url,updated_at,revision,unread,alert_seq)
                        VALUES (:username,:thread_id,:title,:repo,:kind,:reason,:url,:updated_at,:revision,:unread,:alert_seq)''',
                        {**item, 'username': username, 'alert_seq': seq if account else 0})
                if raw is not None:
                    # The baseline query differs from incremental queries: do not reuse its validator.
                    con.execute('INSERT OR REPLACE INTO notification_accounts VALUES (?,?,?)',
                                (username, watermark, headers.get('Last-Modified') if account else None))
                con.execute('''UPDATE notification_monitor SET username=?,checked_at=?,next_check=?,error=NULL,failures=0 WHERE id=1''',
                            (username, stamp(now()), stamp(now() + timedelta(seconds=max(opts['notification_interval'], client.interval)))))
                current_opts = self.settings(con)
            if current_opts['notifications_enabled'] and current_opts['desktop_notifications']:
                desktop_alert(alerts)
            return {'status': 'success', 'new': len(alerts)}
        except Exception as exc:
            if not claimed:
                raise
            with self.db_conn() as con:
                failures = con.execute('SELECT failures FROM notification_monitor WHERE id=1').fetchone()[0] + 1
                delay = max(getattr(exc, 'delay', 0), min(3600, 60 * 2 ** min(failures, 6)))
                con.execute('UPDATE notification_monitor SET error=?,failures=?,next_check=? WHERE id=1',
                            (str(exc), failures, stamp(now() + timedelta(seconds=delay))))
            return {'status': 'error', 'error': str(exc)}
        finally:
            if lock_file:
                lock_file.close()
            self.lock.release()

    def snapshot(self):
        with self.db_conn() as con:
            opts = self.settings(con)
            status = dict(con.execute('SELECT * FROM notification_monitor WHERE id=1').fetchone())
            username = (opts.get('github_username') or status['username']).lower()
            items = [dict(r) for r in con.execute('SELECT * FROM notifications WHERE username=? ORDER BY unread DESC,updated_at DESC LIMIT 200', (username,))]
            unread = con.execute('SELECT count(*) FROM notifications WHERE username=? AND unread=1', (username,)).fetchone()[0]
            latest = con.execute('SELECT COALESCE(MAX(alert_seq),0) FROM notifications WHERE username=?', (username,)).fetchone()[0]
        return {'items': items, 'unread': unread, 'latest': latest, 'username': username,
                'status': status, 'enabled': opts['notifications_enabled'],
                'desktop': opts['desktop_notifications'], 'interval': opts['notification_interval']}

    def mark_read(self, payload):
        # The caller sends the versions it saw, so a racing new reply stays unread.
        items = payload.get('items')
        if not isinstance(items, list) or len(items) > 200 or any(not isinstance(i, dict) or not isinstance(i.get('thread_id'), str) or not isinstance(i.get('revision'), str) for i in items):
            raise ValueError('Expected up to 200 notification IDs and revisions.')
        with self.db_conn() as con:
            username = (self.settings(con).get('github_username') or con.execute('SELECT username FROM notification_monitor WHERE id=1').fetchone()[0]).lower()
            if payload.get('username') != username:
                raise ValueError('The notification account changed. Refresh the inbox.')
            con.executemany('UPDATE notifications SET unread=0 WHERE username=? AND thread_id=? AND revision=?',
                            [(username, i['thread_id'], i['revision']) for i in items])

    def run(self):
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as exc:
                print('Notification monitor:', exc, file=sys.stderr)
            self.stop.wait(5)
