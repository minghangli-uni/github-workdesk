"""Authenticated, single-owner hosted Workdesk. Run behind an HTTPS reverse proxy."""
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

import app as core
from cloud_identity import Identity
from cloud_push import Push

COOKIE = '__Host-workdesk'
FLOW_COOKIE = '__Host-workdesk-oauth'
IMPORT_TABLES = ('tasks', 'events', 'sync_runs', 'snapshots', 'contributions', 'contribution_sync')


def validate_contribution(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid contribution record.')
    for key in ('id', 'repo', 'kind', 'title', 'url', 'state', 'status', 'updated_at', 'last_contributed_at'):
        if not isinstance(data.get(key), str):
            raise ValueError('Incomplete contribution record.')
    core.validate_http_url(data['url'])
    if data['kind'] not in ('pr', 'commit') or (data['kind'] == 'pr' and (type(data.get('number')) is not int or data['number'] <= 0)):
        raise ValueError('Invalid contribution type or PR number.')
    if not isinstance(data.get('roles'), list) or any(not isinstance(role, str) for role in data['roles']):
        raise ValueError('Invalid contribution roles.')
    if not isinstance(data.get('evidence'), list):
        raise ValueError('Invalid contribution evidence.')
    for evidence in data['evidence']:
        if not isinstance(evidence, dict) or any(not isinstance(evidence.get(key), str) for key in ('id', 'type', 'title', 'url', 'date')):
            raise ValueError('Invalid contribution evidence.')
        core.validate_http_url(evidence['url'])
        if not isinstance(evidence.get('roles'), list) or any(not isinstance(role, str) for role in evidence['roles']):
            raise ValueError('Invalid evidence roles.')
        if evidence['type'] == 'commit' and not isinstance(evidence.get('sha'), str):
            raise ValueError('Invalid commit evidence.')


def configuration():
    origin = (os.environ.get('WORKDESK_PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
    url = urlsplit(origin)
    if url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment or url.username or url.password:
        raise ValueError('Set WORKDESK_PUBLIC_URL to the HTTPS origin of your hosted app.')
    values = {key: os.environ.get(env, '').strip() for key, env in {
        'CLIENT_ID': 'GITHUB_CLIENT_ID', 'CLIENT_SECRET': 'GITHUB_CLIENT_SECRET',
        'SECRET_KEY': 'WORKDESK_SECRET_KEY', 'OWNER_ID': 'WORKDESK_OWNER_ID',
    }.items()}
    if not all(values.values()) or len(values['SECRET_KEY']) < 32:
        raise ValueError('Set the GitHub OAuth credentials, WORKDESK_OWNER_ID and a random WORKDESK_SECRET_KEY of at least 32 characters.')
    if not values['OWNER_ID'].isdigit() or int(values['OWNER_ID']) <= 0:
        raise ValueError('WORKDESK_OWNER_ID must be your numeric GitHub account ID.')
    folder = os.environ.get('WORKDESK_DATA_DIR', '')
    if not folder or not Path(folder).is_absolute():
        raise ValueError('Set WORKDESK_DATA_DIR to an absolute path on persistent storage.')
    interval = int(os.environ.get('WORKDESK_SYNC_INTERVAL', '1800'))
    if not 900 <= interval <= 86400:
        raise ValueError('WORKDESK_SYNC_INTERVAL must be 900–86400 seconds.')
    return {**values, 'ORIGIN': origin, 'HOST': url.netloc, 'SYNC_INTERVAL': interval}


class CloudAPI(core.WorkdeskAPI):
    """Use the same task/sync operations after Flask has authenticated each request."""
    def __init__(self):
        # The shared API decodes task IDs once, so preserve WSGI's encoded path.
        from urllib.parse import quote
        self.path = quote(request.path, safe='/')
        if request.query_string:
            self.path += '?' + request.query_string.decode('ascii')

    def allowed_host(self):
        return True  # Mandatory Flask before_request checks already ran.

    def read_payload(self):
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object.')
        return payload

    def send_json(self, data, code=200):
        return jsonify(data), code

    def send_binary(self, data, name, content_type):
        return send_file(io.BytesIO(data), mimetype=content_type, as_attachment=True, download_name=name)

    def handle_error(self, exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, KeyError):
            return jsonify(error='Item not found.'), 404
        if isinstance(exc, (ValueError, TypeError)):
            return jsonify(error=str(exc)), 400
        return jsonify(error='The request could not be completed. Retry or check your GitHub connection.'), 500


def import_backup(payload, login):
    if not isinstance(payload, dict) or not isinstance(payload.get('tasks'), list):
        raise ValueError('Choose a Workdesk JSON export.')
    if not core.SYNC_LOCK.acquire(blocking=False):
        raise ValueError('Wait for the current sync to finish before importing.')
    try:
        with core.db_conn() as con:
            con.execute('BEGIN IMMEDIATE')
            if any(con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in IMPORT_TABLES):
                raise ValueError('Import is available only before your first sync or task. Existing hosted work will not be overwritten.')
            total = 0
            for table in IMPORT_TABLES:
                rows = payload.get(table, [])
                if not isinstance(rows, list) or len(rows) > 50000:
                    raise ValueError('Invalid or oversized backup.')
                schema = {r['name']: dict(r) for r in con.execute(f'PRAGMA table_info({table})')}
                allowed = set(schema)
                for row in rows:
                    if not isinstance(row, dict) or not row or set(row) - allowed:
                        raise ValueError('Unsupported backup fields.')
                    for field, value in row.items():
                        if value is None and not schema[field]['notnull']:
                            continue
                        if schema[field]['type'] == 'TEXT' and not isinstance(value, str):
                            raise ValueError('Invalid text in backup.')
                        if schema[field]['type'] == 'INTEGER' and type(value) is not int:
                            raise ValueError('Invalid number in backup.')
                        if value and (field.endswith('_at') or field in ('due_date', 'snoozed_until')) and core.to_dt(value) is None:
                            raise ValueError('Invalid date in backup.')
                    if row.get('username', login).lower() != login.lower():
                        raise ValueError('This backup contains contributions from a different GitHub account.')
                    for field in ('url',):
                        if field in row:
                            core.validate_http_url(row[field])
                    for field in ('labels', 'errors', 'warnings'):
                        if field in row:
                            parsed = json.loads(row[field])
                            if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
                                raise ValueError('Invalid backup data.')
                    if table == 'contributions':
                        data = json.loads(row['data'])
                        validate_contribution(data)
                        if any(data[field] != row[field] for field in ('id', 'repo', 'kind')):
                            raise ValueError('Contribution fields do not match.')
                        if not core.contributions.REPO.fullmatch(row['repo']):
                            raise ValueError('Invalid contribution repository.')
                    columns = ','.join('"' + k + '"' for k in row)
                    con.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
                    total += 1
            settings = payload.get('settings', [])
            if not isinstance(settings, list):
                raise ValueError('Invalid backup settings.')
            known = core.get_settings(con)
            raw = {r['key']: json.loads(r['value']) for r in settings
                   if isinstance(r, dict) and r.get('key') in {*known, 'last_synced_user'}}
            owner = raw.get('last_synced_user') or raw.get('github_username')
            if owner and owner.lower() != login.lower():
                raise ValueError('This backup belongs to a different GitHub account.')
            # Validate preferences with the same rules as interactive settings.
            safe = core.contributions.validate_settings(raw)
            if 'lookback_days' in raw:
                if type(raw['lookback_days']) is not int or not 30 <= raw['lookback_days'] <= 1825:
                    raise ValueError('Invalid history setting in backup.')
                safe['lookback_days'] = raw['lookback_days']
            if 'focus_orgs' in raw:
                orgs = raw['focus_orgs']
                if not isinstance(orgs, list) or len(orgs) > 20 or any(not isinstance(x, str) or len(x) > 60 for x in orgs):
                    raise ValueError('Invalid organizations in backup.')
                safe['focus_orgs'] = orgs
            for key in ('notifications_enabled', 'desktop_notifications'):
                if key in raw and type(raw[key]) is bool:
                    safe[key] = raw[key]
            for key, value in safe.items():
                con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))
            return total
    except (sqlite3.Error, KeyError, TypeError, AttributeError):
        raise ValueError('Invalid Workdesk backup. No records were imported.') from None
    finally:
        core.SYNC_LOCK.release()


def create_app():
    config = configuration()
    folder = core.default_data_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = Identity(folder, config)
    core.init_db()
    core.HOSTED_TOKEN_PROVIDER = identity.token
    push = Push(identity, core.db_conn)
    web = Flask(__name__, static_folder=None)
    web.config.update(MAX_CONTENT_LENGTH=256 * 1024, TRUSTED_HOSTS=[urlsplit(config['ORIGIN']).hostname])
    web.extensions.update(workdesk_identity=identity, workdesk_push=push, workdesk_config=config)

    @web.before_request
    def protect():
        if request.host != config['HOST']:
            abort(400)
        if request.path == '/api/health' and request.method in ('GET', 'HEAD'):
            return None
        if request.path.startswith('/static/') or request.path in ('/manifest.webmanifest', '/sw.js', '/login', '/auth/login', '/auth/callback'):
            if request.method not in ('GET', 'HEAD'):
                abort(405)
            return None
        g.identity_session = identity.session(request.cookies.get(COOKIE))
        if not g.identity_session:
            if request.path.startswith('/api/'):
                return jsonify(error='Sign in to Workdesk.', login_url='/login'), 401
            return redirect('/login')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('Origin') != config['ORIGIN']:
                return jsonify(error='Request origin could not be verified.'), 403
            csrf = request.headers.get('X-Workdesk-CSRF', '')
            if not secrets.compare_digest(csrf, g.identity_session['csrf']):
                return jsonify(error='Your session changed. Reload Workdesk and try again.'), 403
        if request.path == '/api/import':
            request.max_content_length = 20 * 1024 * 1024

    @web.after_request
    def security_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @web.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @web.get('/api/health')
    def health():
        return jsonify(ok=True)

    @web.get('/login')
    def login():
        return send_from_directory(core.ROOT / 'static', 'login.html')

    @web.get('/auth/login')
    def begin_login():
        try:
            state, url = identity.begin()
        except RuntimeError as exc:
            return Response(str(exc), status=429, mimetype='text/plain')
        response = redirect(url)
        response.set_cookie(FLOW_COOKIE, state, max_age=600, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @web.get('/auth/callback')
    def callback():
        try:
            cookie, login = identity.finish(request.args.get('state'), request.cookies.get(FLOW_COOKIE), request.args.get('code'))
            with core.db_conn() as con:
                con.execute("UPDATE settings SET value=? WHERE key='github_username'", (json.dumps(login),))
                con.execute('UPDATE notification_monitor SET next_check=NULL WHERE id=1')
            old = identity.session(request.cookies.get(COOKIE))
            if old:
                # Keep subscriptions through reconnect; rebind them to the new session.
                from cloud_identity import digest
                with identity.db() as con:
                    con.execute('UPDATE subscriptions SET session_id=? WHERE session_id=?', (digest(cookie), old['id']))
                    con.execute('DELETE FROM sessions WHERE id=?', (old['id'],))
            response = redirect('/')
            response.set_cookie(COOKIE, cookie, max_age=30 * 86400, secure=True, httponly=True, samesite='Lax', path='/')
        except (ValueError, RuntimeError, PermissionError) as exc:
            response = Response(str(exc) + '\nReturn to /login to try again.', status=403, mimetype='text/plain')
        response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @web.post('/auth/logout')
    def logout():
        identity.logout(g.identity_session)
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @web.get('/')
    def index():
        return send_from_directory(core.ROOT / 'static', 'index.html')

    @web.get('/static/<path:name>')
    def static_file(name):
        return send_from_directory(core.ROOT / 'static', name)

    @web.get('/manifest.webmanifest')
    def manifest():
        return send_from_directory(core.ROOT / 'static', 'manifest.webmanifest', mimetype='application/manifest+json')

    @web.get('/sw.js')
    def service_worker():
        return send_from_directory(core.ROOT / 'static', 'sw.js', mimetype='text/javascript')

    @web.get('/api/dashboard')
    def dashboard():
        data = core.dashboard({})
        data.update(hosted=True, csrf_token=g.identity_session['csrf'], platform='cloud',
                    data_dir='Hosted storage', cloud={'login': identity.get('login'),
                    'sync_interval': config['SYNC_INTERVAL'], 'push': push.status()})
        return jsonify(data)

    @web.post('/api/import')
    def import_data():
        try:
            return jsonify(imported=import_backup(request.get_json(), identity.get('login')))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @web.post('/api/push/subscribe')
    def subscribe():
        try:
            push.subscribe(request.get_json(), g.identity_session['id'])
            return jsonify(ok=True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @web.post('/api/push/unsubscribe')
    def unsubscribe():
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='Expected a JSON object.'), 400
        try:
            push.unsubscribe(data.get('endpoint'))
            return jsonify(ok=True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @web.route('/api/<path:path>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
    def api(path):
        if path in ('reminder', 'notification-watcher'):
            return jsonify(error='Hosted Workdesk runs background checks on the server.'), 404
        if path == 'settings' and request.method == 'POST':
            body = request.get_json()
            if not isinstance(body, dict):
                return jsonify(error='Expected a JSON object.'), 400
            if 'github_username' in body and body['github_username'] != identity.get('login'):
                return jsonify(error='The hosted account is set by GitHub sign-in.'), 400
        return getattr(CloudAPI(), 'do_' + ('GET' if request.method == 'HEAD' else request.method))()

    return web


def run_background(web, stop):
    identity, push = web.extensions['workdesk_identity'], web.extensions['workdesk_push']
    sync_interval = web.extensions['workdesk_config']['SYNC_INTERVAL']
    last_attempt = 0
    while not stop.is_set():
        if identity.get('github_token'):
            try:
                core.NOTIFICATIONS.poll()
                push.flush()
                with core.db_conn() as con:
                    run = con.execute('SELECT started_at FROM sync_runs ORDER BY id DESC LIMIT 1').fetchone()
                # Leave a fresh workspace empty so an existing local export can be imported first.
                started = core.to_dt(run['started_at']) if run else None
                if started and time.time() - max(started.timestamp(), last_attempt) >= sync_interval and not core.SYNC_STATE['running']:
                    last_attempt = time.time()
                    def sync():
                        try:
                            core.sync_github()
                        except Exception:
                            pass  # The sync report records the failure for the UI.
                    threading.Thread(target=sync, daemon=True).start()
            except Exception:
                # Do not put bearer tokens or push endpoints in hosting logs.
                print('Background update could not finish; it will retry.', flush=True)
        stop.wait(5)


def main():
    from waitress import serve
    # One process/instance owns SQLite, the notification poller and push checkpoints.
    configuration()
    core.default_data_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    lock = open(core.default_data_dir() / 'cloud.lock', 'a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    try:
        web = create_app()
        threading.Thread(target=run_background, args=(web, stop), daemon=True).start()
        serve(web, host='0.0.0.0', port=int(os.environ.get('PORT', '10000')), threads=8,
              max_request_body_size=20 * 1024 * 1024, clear_untrusted_proxy_headers=True)
    finally:
        stop.set()
        lock.close()


if __name__ == '__main__':
    main()
