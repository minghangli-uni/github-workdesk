"""Private HTTPS access through Tailscale Serve; all work remains in the local DB.

The second listener is loopback-only. Serve supplies the authenticated identity;
only the Mac owner's untagged devices are accepted, with strict write origins.
This module never enables Funnel or changes another service's Serve mapping.
"""
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlparse

GATEWAY_PORT = 8766
HTTPS_PORT = 8443
TARGET = f'http://127.0.0.1:{GATEWAY_PORT}'


def find_cli():
    candidates = [shutil.which('tailscale'), '/usr/local/bin/tailscale',
                  '/opt/homebrew/bin/tailscale',
                  '/Applications/Tailscale.app/Contents/MacOS/Tailscale',
                  str(Path.home() / 'Applications/Tailscale.app/Contents/MacOS/Tailscale')]
    return next((p for p in candidates if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)


def run_cli(*args):
    cli = find_cli()
    if not cli:
        raise ValueError('Install Tailscale on this Mac, open it and sign in, then try again.')
    try:
        # Force CLI mode even when Workdesk was started without a terminal.
        result = subprocess.run([cli, *args], capture_output=True, text=True, timeout=15,
                                env={**os.environ, 'TAILSCALE_BE_CLI': '1'})
    except subprocess.TimeoutExpired:
        raise ValueError('Tailscale did not finish. Open Tailscale, confirm it is connected, and enable HTTPS certificates in its DNS settings. Then retry.') from None
    if result.returncode:
        raise ValueError('Tailscale could not complete setup. Check that it is connected and HTTPS certificates are enabled in its DNS settings.')
    try:
        return json.loads(result.stdout) if '--json' in args else None
    except json.JSONDecodeError:
        raise ValueError('Tailscale returned an unreadable status. Update Tailscale and retry.') from None


def valid_config(config):
    if not isinstance(config, dict) or config.get('enabled') is not True:
        return False
    host = config.get('host', '')
    owner = config.get('owner', '')
    return (isinstance(host, str) and len(host) <= 253
            and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\.ts\.net', host)
            and isinstance(owner, str) and 0 < len(owner) <= 320
            and not any(ord(c) < 32 for c in owner))


def current_identity():
    status = run_cli('status', '--json')
    if status.get('BackendState') != 'Running':
        raise ValueError('Open Tailscale on this Mac, sign in and connect, then retry.')
    device = status.get('Self') or {}
    user = (status.get('User') or {}).get(str(device.get('UserID')), {})
    config = {'enabled': True, 'host': device.get('DNSName', '').rstrip('.').lower(),
              'owner': user.get('LoginName', '')}
    if device.get('Tags') or not valid_config(config):
        raise ValueError('Use a Mac signed in to your own Tailscale account with MagicDNS enabled. Tagged devices are not supported.')
    return config


def authority(config):
    return f'{config["host"]}:{HTTPS_PORT}'


def port_configs(config):
    yield config
    for foreground in (config.get('Foreground') or {}).values():
        yield from port_configs(foreground)


def mapping_matches(serve, config):
    host = authority(config)
    return (serve.get('TCP', {}).get(str(HTTPS_PORT)) == {'HTTPS': True}
            and serve.get('Web', {}).get(host) == {'Handlers': {'/': {'Proxy': TARGET}}}
            and {h for h in serve.get('Web', {}) if h.endswith(f':{HTTPS_PORT}')} == {host}
            and not any(enabled for h, enabled in serve.get('AllowFunnel', {}).items() if h.endswith(f':{HTTPS_PORT}'))
            and not any(str(HTTPS_PORT) in c.get('TCP', {}) or
                        any(h.endswith(f':{HTTPS_PORT}') for h in c.get('Web', {}))
                        for c in list(port_configs(serve))[1:]))


def port_used(serve):
    return any(str(HTTPS_PORT) in c.get('TCP', {}) or
               any(h.endswith(f':{HTTPS_PORT}') for h in c.get('Web', {}))
               or any(enabled for h, enabled in c.get('AllowFunnel', {}).items() if h.endswith(f':{HTTPS_PORT}'))
               for c in port_configs(serve))


def gateway_handler(api, access):
    class PhoneHandler(api, BaseHTTPRequestHandler):
        local_storage = True

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def allowed_host(self):
            config = access.config
            if not config or self.client_address[0] != '127.0.0.1':
                return False
            hosts = self.headers.get_all('Host', [])
            identities = self.headers.get_all('Tailscale-User-Login', [])
            if len(hosts) != 1 or hosts[0].lower() != authority(config) or len(identities) != 1:
                return False
            try:
                login = str(make_header(decode_header(identities[0])))
            except (ValueError, LookupError, UnicodeError):
                return False
            return login == config['owner']

        def end_headers(self):
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.send_header('Referrer-Policy', 'same-origin')
            super().end_headers()

        def send_json(self, data, code=200):
            if isinstance(data, dict) and 'tasks' in data and 'settings' in data:
                data = {**data, 'phone_access': {'remote': True, 'url': f'https://{authority(access.config)}'}}
            return super().send_json(data, code)

        def read_payload(self):
            origin = f'https://{authority(access.config)}'
            if (self.headers.get_all('Origin', []) != [origin]
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'):
                raise ValueError('Open Workdesk from your private HTTPS address before saving changes.')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('JSON requests are required.')
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
                raise ValueError('A single Content-Length is required.')
            size = int(lengths[0])
            if not 0 <= size <= 256 * 1024:
                raise ValueError('Request body too large or invalid.')
            data = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('A JSON object is required.')
            return data

        def do_GET(self):
            if not self.allowed_host():
                return self.send_json({'error': 'Connect Tailscale on this device using the same account as your Mac.'}, 403)
            extra = {'/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
                     '/sw.js': ('sw.js', 'text/javascript'),
                     '/static/offline.html': ('offline.html', 'text/html; charset=utf-8')}
            path = urlparse(self.path).path
            if path in extra:
                return self.serve_static(*extra[path])
            return super().do_GET()

        def do_POST(self):
            if not self.allowed_host():
                return self.send_json({'error': 'Your Tailscale account is required.'}, 403)
            if urlparse(self.path).path in {'/api/phone-access', '/api/open-data-folder',
                                           '/api/notification-watcher', '/api/reminder'}:
                return self.send_json({'error': 'Manage this setting from Workdesk on your Mac.'}, 403)
            return super().do_POST()

    return PhoneHandler


class Access:
    def __init__(self, data_dir, api):
        self.data_dir = data_dir
        self.api = api
        self.config = None
        self.server = None
        self.lock = threading.Lock()
        self.message = ''
        self.verified = False

    @property
    def config_path(self):
        return self.data_dir() / 'phone-access.json'

    def save_config(self, value):
        folder = self.data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.phone-access-', dir=folder)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(value, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.config_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def start_gateway(self):
        if not self.server:
            server = ThreadingHTTPServer(('127.0.0.1', GATEWAY_PORT), gateway_handler(self.api, self))
            self.server = server
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def start(self):
        try:
            if self.config_path.exists():
                config = json.loads(self.config_path.read_text())
                if config.get('enabled'):
                    if not valid_config(config):
                        raise ValueError('Saved phone setup is invalid. Disable and enable iPhone access again.')
                    self.start_gateway()
                    self.config = config
                    self.message = 'Phone access is configured. Check connection to verify Tailscale is ready.'
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.message = f'Phone access needs attention: {exc}'

    def close(self):
        self.config = None
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def info(self):
        return {'enabled': bool(self.config), 'installed': bool(find_cli()),
                'verified': self.verified, 'message': self.message,
                'url': f'https://{authority(self.config)}' if self.config else None}

    def change(self, action):
        if action not in ('enable', 'disable', 'check'):
            raise ValueError('Unknown phone access action.')
        with self.lock:
            self.verified = False
            if action == 'disable':
                old = self.config
                # Persist revocation first; old identities stop being accepted immediately.
                self.save_config({'enabled': False})
                self.close()
                self.message = 'iPhone access is off. Your saved work is still on this Mac.'
                if old:
                    try:
                        serve = run_cli('serve', 'status', '--json') or {}
                        if mapping_matches(serve, old):
                            run_cli('serve', f'--https={HTTPS_PORT}', 'off')
                    except (OSError, ValueError):
                        self.message += ' The private Tailscale route could not be removed, but Workdesk access is blocked.'
                return self.info()
            try:
                created_route = False
                config = current_identity()
                serve = run_cli('serve', 'status', '--json') or {}
                if action == 'check':
                    if self.config and config != self.config:
                        raise ValueError('Your Tailscale account or Mac address changed. Disable iPhone access and enable it again.')
                    if self.config and not mapping_matches(serve, config):
                        raise ValueError('The private Tailscale route is missing or changed. Disable iPhone access and enable it again.')
                    self.verified = bool(self.config and self.server)
                    self.message = 'Private connection is configured. Open the address on your iPhone to test it.' if self.verified else 'Tailscale is connected. You can now enable iPhone access.'
                else:
                    if self.config and config != self.config:
                        raise ValueError('Disable iPhone access before switching Tailscale accounts or addresses.')
                    if port_used(serve) and not (self.config == config and mapping_matches(serve, config)):
                        raise ValueError('Tailscale port 8443 is already in use. Remove that mapping in Tailscale or use another Mac; Workdesk left it unchanged.')
                    self.start_gateway()
                    # Reject every request until Serve is private, verified and saved.
                    created_route = not port_used(serve)
                    run_cli('serve', '--bg', f'--https={HTTPS_PORT}', '--yes', TARGET)
                    if not mapping_matches(run_cli('serve', 'status', '--json') or {}, config):
                        raise ValueError('Tailscale did not confirm a private HTTPS route. Phone access was not enabled.')
                    self.save_config(config)
                    self.config = config
                    self.verified = True
                    self.message = 'Private connection is configured. Open the address on your iPhone to test it.'
            except (OSError, ValueError) as exc:
                self.message = str(exc)
                if not self.config:
                    self.close()
                    if created_route:
                        try:
                            if mapping_matches(run_cli('serve', 'status', '--json') or {}, config):
                                run_cli('serve', f'--https={HTTPS_PORT}', 'off')
                        except (OSError, ValueError):
                            self.message += ' If retry reports port 8443 in use, remove its route in Tailscale first.'
                raise ValueError(self.message) from None
            return self.info()
