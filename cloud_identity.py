"""Owner-only OAuth, opaque sessions, and encrypted server credentials.

Identity data is deliberately separate from the exportable Workdesk database.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def github_json(url, data=None, token=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'github-workdesk'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        with urlopen(Request(url, data=urlencode(data).encode() if data is not None else None,
                             headers=headers), timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise RuntimeError('GitHub sign-in is unavailable. Please try signing in again.') from None
    if not isinstance(result, dict) or result.get('error'):
        raise RuntimeError('GitHub authorization failed. Please try signing in again.')
    return result


class Identity:
    def __init__(self, folder, config):
        self.path = folder / 'cloud-identity.sqlite3'
        self.config = config
        key = hashlib.sha256(('workdesk-credentials-v1:' + config['SECRET_KEY']).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.token_lock = threading.RLock()
        with self.db() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth (state TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
                    last_seq INTEGER NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0, error TEXT
                );
            ''')
            owner = con.execute("SELECT value FROM metadata WHERE key='owner_id'").fetchone()
            if owner and owner[0] != str(config['OWNER_ID']):
                raise ValueError('This data directory belongs to a different GitHub account.')
            con.execute("INSERT OR IGNORE INTO metadata VALUES ('owner_id', ?)", (str(config['OWNER_ID']),))
            check = con.execute("SELECT value FROM metadata WHERE key='key_check'").fetchone()
            if check:
                try:
                    self.decrypt(check[0])
                except Exception:
                    raise ValueError('WORKDESK_SECRET_KEY does not match this data directory. Restore the original secret.') from None
            else:
                con.execute("INSERT INTO metadata VALUES ('key_check', ?)", (self.encrypt('workdesk'),))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def get(self, key):
        with self.db() as con:
            row = con.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def put(self, key, value):
        with self.db() as con:
            con.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, value))

    def begin(self):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self.db() as con:
            con.execute('DELETE FROM oauth WHERE expires<?', (time.time(),))
            # Bound storage even when an unauthenticated visitor starts many flows.
            if con.execute('SELECT count(*) FROM oauth').fetchone()[0] >= 100:
                raise RuntimeError('Too many sign-in attempts. Try again in a few minutes.')
            con.execute('INSERT INTO oauth VALUES (?,?,?)',
                        (digest(state), self.encrypt(verifier), time.time() + 600))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        url = 'https://github.com/login/oauth/authorize?' + urlencode({
            'client_id': self.config['CLIENT_ID'], 'redirect_uri': self.config['ORIGIN'] + '/auth/callback',
            'scope': 'repo notifications read:user read:org user:email', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'allow_signup': 'false',
        })
        return state, url

    def finish(self, state, cookie, code):
        if not state or not cookie or not secrets.compare_digest(state, cookie) or not code:
            raise ValueError('Sign-in expired or could not be verified. Start again.')
        with self.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM oauth WHERE state=?', (digest(state),)).fetchone()
            con.execute('DELETE FROM oauth WHERE state=?', (digest(state),))
        if not row or row['expires'] < time.time():
            raise ValueError('Sign-in expired or was already used. Start again.')
        token = github_json('https://github.com/login/oauth/access_token', {
            'client_id': self.config['CLIENT_ID'], 'client_secret': self.config['CLIENT_SECRET'],
            'code': code, 'code_verifier': self.decrypt(row['verifier']),
            'redirect_uri': self.config['ORIGIN'] + '/auth/callback',
        })
        if not token.get('access_token'):
            raise ValueError('GitHub did not return an access token. Start again.')
        profile = github_json('https://api.github.com/user', token=token['access_token'])
        if str(profile.get('id')) != str(self.config['OWNER_ID']):
            raise PermissionError('This Workdesk belongs to a different GitHub account.')
        import re
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', profile.get('login', '')):
            raise ValueError('GitHub did not return a valid account name.')
        self.store_token(token)
        self.put('login', profile['login'])
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as con:
            con.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            con.execute('INSERT INTO sessions VALUES (?,?,?)',
                        (digest(session_id), csrf, time.time() + 30 * 86400))
        return session_id, profile['login']

    def session(self, cookie):
        if not cookie:
            return None
        with self.db() as con:
            row = con.execute('SELECT * FROM sessions WHERE id=? AND expires>?',
                              (digest(cookie), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, session):
        with self.db() as con:
            con.execute('DELETE FROM sessions WHERE id=?', (session['id'],))
            con.execute('DELETE FROM subscriptions WHERE session_id=?', (session['id'],))

    def store_token(self, data):
        value = dict(data)
        if value.get('expires_in'):
            value['expires_at'] = time.time() + int(value['expires_in'])
        self.put('github_token', self.encrypt(value))

    def token(self):
        with self.token_lock:
            stored = self.get('github_token')
            if not stored:
                raise RuntimeError('Sign in with GitHub to start automatic updates.')
            token = self.decrypt(stored)
            if token.get('expires_at', float('inf')) < time.time() + 60:
                if not token.get('refresh_token'):
                    raise RuntimeError('GitHub authorization expired. Sign in again to reconnect.')
                token = github_json('https://github.com/login/oauth/access_token', {
                    'client_id': self.config['CLIENT_ID'], 'client_secret': self.config['CLIENT_SECRET'],
                    'grant_type': 'refresh_token', 'refresh_token': token['refresh_token'],
                })
                if not token.get('access_token'):
                    raise RuntimeError('GitHub authorization expired. Sign in again to reconnect.')
                self.store_token(token)
            return token['access_token']
