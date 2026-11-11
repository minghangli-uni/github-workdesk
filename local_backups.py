"""Consistent snapshots of the local Workdesk database. No network access."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid

AUTOMATIC_COPIES = 7
AUTOMATIC_INTERVAL = 24 * 60 * 60
NAME = re.compile(r'workdesk-(auto|manual)-(\d{8}T\d{12}Z)-[0-9a-f]{8}\.sqlite3\Z')


class Backups:
    def __init__(self, db_conn, data_dir):
        self.db_conn = db_conn
        self.data_dir = data_dir
        self.lock = threading.Lock()

    @property
    def directory(self):
        return Path(self.data_dir()) / 'backups'

    def inventory(self):
        records = []
        if not self.directory.exists():
            return records
        for path in self.directory.iterdir():
            match = NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                created = datetime.strptime(match[2], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            records.append({'name': path.name, 'kind': match[1], 'created_at': created.isoformat(),
                            'size_bytes': path.stat().st_size})
        return sorted(records, key=lambda row: row['created_at'], reverse=True)

    def info(self):
        error = None
        try:
            records = self.inventory()
            path = Path(self.data_dir()) / 'backup-status.json'
            status = json.loads(path.read_text()) if path.exists() else {}
            error = status.get('error')
        except (OSError, ValueError, TypeError, AttributeError):
            records = []
            error = 'Could not read the local backup status. Try saving a new backup.'
        return {'mode': 'local', 'database_path': str(Path(self.data_dir()) / 'workdesk.sqlite3'),
                'backup_directory': str(self.directory), 'automatic_copies': AUTOMATIC_COPIES,
                'backup_count': len(records), 'latest_backup': records[0] if records else None,
                'error': error}

    def record_status(self, error=None):
        folder = Path(self.data_dir())
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', prefix='.backup-status-', dir=folder, delete=False) as output:
            temporary = Path(output.name)
            json.dump({'error': error}, output)
        try:
            os.replace(temporary, folder / 'backup-status.json')
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def process_lock(self):
        folder = Path(self.data_dir())
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / 'backup.lock', 'a+b') as handle:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b'0'); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Another local backup is in progress. Try again shortly.') from None
            yield

    def create(self, automatic=False, clock=None):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A local backup is already in progress. Try again shortly.')
        temporary = None
        try:
            with self.process_lock():
                now = clock or datetime.now(timezone.utc)
                records = self.inventory()
                automatic_records = [row for row in records if row['kind'] == 'auto']
                if automatic and automatic_records:
                    age = (now - datetime.fromisoformat(automatic_records[0]['created_at'])).total_seconds()
                    if age < AUTOMATIC_INTERVAL:
                        return {'status': 'not_due'}
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                kind = 'auto' if automatic else 'manual'
                name = f'workdesk-{kind}-{now.strftime("%Y%m%dT%H%M%S%fZ")}-{uuid.uuid4().hex[:8]}.sqlite3'
                destination = self.directory / name
                with tempfile.NamedTemporaryFile(prefix='.workdesk-backup-', suffix='.partial',
                                                 dir=self.directory, delete=False) as output:
                    temporary = Path(output.name)
                deadline = time.monotonic() + 60
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise RuntimeError('The database stayed busy for too long. Try saving a backup again.')
                # SQLite's backup API includes committed WAL data and supports concurrent writes.
                with closing(self.db_conn()) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target, pages=256, progress=progress, sleep=0.05)
                    target.execute('PRAGMA journal_mode=DELETE')
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('The backup could not be verified. The original database was kept.')
                with open(temporary, 'rb') as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                temporary = None
                # Only rotate snapshots created by this automatic-backup feature.
                # Manual copies and unrelated files are never removed.
                if automatic:
                    for row in automatic_records[AUTOMATIC_COPIES - 1:]:
                        (self.directory / row['name']).unlink(missing_ok=True)
                self.record_status()
                return {'status': 'saved', 'name': name, 'path': str(destination), 'storage': self.info()}
        except Exception as exc:
            try:
                self.record_status(str(exc))
            except OSError:
                pass
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + '-journal').unlink(missing_ok=True)
            self.lock.release()

    def maybe_create(self):
        try:
            return self.create(automatic=True)
        except Exception:
            # A failed backup must not stop normal local saves or GitHub checks.
            return {'status': 'error'}

    def run(self, stop):
        while not stop.is_set():
            self.maybe_create()
            stop.wait(600)
