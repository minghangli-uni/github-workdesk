"""Durable per-device Web Push delivery from the existing notification inbox."""
import base64
import json
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import requests
from pywebpush import webpush

from cloud_identity import digest


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def validate_subscription(value):
    try:
        endpoint = value['endpoint']
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = host in {'web.push.apple.com', 'fcm.googleapis.com', 'updates.push.services.mozilla.com'}
        allowed = allowed or host.endswith('.push.apple.com')
        if (len(endpoint) > 2048 or url.scheme != 'https' or not allowed or
                url.username or url.password or url.port not in (None, 443) or url.fragment):
            raise ValueError()
        keys = {key: value['keys'][key] for key in ('auth', 'p256dh')}
        decoded = {}
        for key, item in keys.items():
            if not isinstance(item, str) or len(item) > 100:
                raise ValueError()
            decoded[key] = base64.b64decode(item + '=' * (-len(item) % 4), altchars=b'-_', validate=True)
        if len(decoded['auth']) != 16 or len(decoded['p256dh']) != 65:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
        return {'endpoint': endpoint, 'keys': keys}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('Invalid push subscription. Use Safari, Chrome, or Firefox and enable notifications again.') from None


class NoRedirectSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(*args, **kwargs)


class Push:
    def __init__(self, identity, db_conn):
        self.identity, self.db_conn = identity, db_conn
        saved = identity.get('vapid_key')
        if saved:
            key = serialization.load_der_private_key(base64.b64decode(identity.decrypt(saved)), password=None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            raw = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            identity.put('vapid_key', identity.encrypt(base64.b64encode(raw).decode()))
        self.private_key = base64.urlsafe_b64encode(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()
        self.public_key = b64(key.public_key().public_bytes(serialization.Encoding.X962,
                                                          serialization.PublicFormat.UncompressedPoint))

    def subscribe(self, payload, session_id):
        subscription = validate_subscription(payload)
        with self.db_conn() as con:
            latest = con.execute('SELECT COALESCE(MAX(alert_seq),0) FROM notifications').fetchone()[0]
        key = digest(subscription['endpoint'])
        with self.identity.db() as con:
            con.execute('BEGIN IMMEDIATE')
            old = con.execute('SELECT id FROM subscriptions WHERE id=?', (key,)).fetchone()
            if not old and con.execute('SELECT count(*) FROM subscriptions').fetchone()[0] >= 10:
                raise ValueError('Up to 10 devices can receive alerts. Turn off alerts on another device first.')
            con.execute('''INSERT INTO subscriptions(id,session_id,payload,last_seq) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id, payload=excluded.payload,
                error=NULL, retry_at=0, failures=0''',
                        (key, session_id, self.identity.encrypt(subscription), latest))

    def unsubscribe(self, endpoint):
        if not isinstance(endpoint, str):
            raise ValueError('A device subscription is required.')
        with self.identity.db() as con:
            con.execute('DELETE FROM subscriptions WHERE id=?', (digest(endpoint),))

    def status(self):
        with self.identity.db() as con:
            rows = con.execute('SELECT error FROM subscriptions').fetchall()
        return {'public_key': self.public_key, 'devices': len(rows),
                'errors': sorted(set(row['error'] for row in rows if row['error']))}

    def flush(self):
        with self.identity.db() as con:
            devices = con.execute('SELECT * FROM subscriptions WHERE retry_at<=?', (time.time(),)).fetchall()
        with self.db_conn() as con:
            settings = {r['key']: json.loads(r['value']) for r in con.execute('SELECT * FROM settings')}
            latest = con.execute('SELECT COALESCE(MAX(alert_seq),0) FROM notifications').fetchone()[0]
            unread = [r['alert_seq'] for r in con.execute('SELECT alert_seq FROM notifications WHERE unread=1')]
        for device in devices:
            if device['last_seq'] >= latest:
                continue
            count = sum(seq > device['last_seq'] for seq in unread)
            enabled = settings['notifications_enabled'] and settings['desktop_notifications']
            if count and enabled:
                try:
                    # Generic lock-screen content keeps repository names and titles private.
                    with NoRedirectSession() as http:
                        result = webpush(subscription_info=validate_subscription(self.identity.decrypt(device['payload'])),
                            data=json.dumps({'title': 'GitHub Workdesk',
                                             'body': f'{count} new GitHub update' + ('' if count == 1 else 's') + '. Open your inbox.',
                                             'tag': 'workdesk-' + str(latest), 'url': '/?view=notifications'}),
                            vapid_private_key=self.private_key,
                            vapid_claims={'sub': self.identity.config['ORIGIN']},
                            ttl=3600, timeout=15, requests_session=http)
                    if not 200 <= result.status_code < 300:
                        raise RuntimeError('Push service did not accept the notification.')
                except Exception as exc:
                    response = getattr(exc, 'response', None)
                    status = getattr(exc, 'status_code', None) or getattr(response, 'status_code', None)
                    with self.identity.db() as con:
                        if status in (404, 410):
                            con.execute('DELETE FROM subscriptions WHERE id=?', (device['id'],))
                        else:
                            delay = min(3600, 60 * 2 ** min(device['failures'], 6))
                            con.execute('UPDATE subscriptions SET failures=failures+1,retry_at=?,error=? WHERE id=?',
                                (time.time() + delay, 'A device alert could not be delivered. Retrying automatically.', device['id']))
                    continue
            with self.identity.db() as con:
                con.execute('UPDATE subscriptions SET last_seq=?,failures=0,retry_at=0,error=NULL WHERE id=?',
                            (latest, device['id']))
