"""Make a freshly mounted data disk writable, then drop root before serving HTTP."""
import os
from pathlib import Path
import sys


def main():
    if os.getuid() == 0:
        folder = Path(os.environ.get('WORKDESK_DATA_DIR', '/var/lib/workdesk'))
        if not folder.is_absolute() or folder.resolve() in (Path('/'), Path('/opt'), Path('/opt/workdesk')):
            raise ValueError('Choose a dedicated absolute data directory.')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(folder, 10001, 10001)
        os.chmod(folder, 0o700)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execv(sys.executable, [sys.executable, '/opt/workdesk/cloud.py'])


if __name__ == '__main__':
    main()
