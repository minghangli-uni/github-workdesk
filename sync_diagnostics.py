"""Keep bounded discovery coverage separate from requests that actually failed."""
import json


def outcome(errors, warnings):
    return 'partial' if errors else 'limited' if warnings else 'success'


def legacy_coverage_note(message):
    # Match only known messages from older releases. Unknown failures remain errors.
    return message.startswith((
        'Verified email lookup unavailable;',
        'Open-PR scans limited to ',
        'PR association checks limited to ',
        'Contribution scan limited to ',
        'Contribution scan reached its request limit;',
        'Repository discovery was limited to ',
        'Co-author search was limited to ',
        'GitHub returned incomplete contribution search results.',
        'A contribution search exceeded GitHub',
        'Incomplete contribution list:',
    )) or (message.startswith('PR ') and ' exceeds GitHub’s 250-commit PR limit' in message)


def migrate(con, table):
    if table not in ('sync_runs', 'contribution_sync'):
        raise ValueError('Unsupported diagnostics table')
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
    if 'warnings' in columns:
        return
    con.execute(f"ALTER TABLE {table} ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'")
    key = 'id' if table == 'sync_runs' else 'username'
    rows = con.execute(f'SELECT * FROM {table}').fetchall()
    for row in rows:
        errors, warnings = [], []
        for message in json.loads(row['errors']):
            contribution_message = message.removeprefix('Contributions: ')
            eligible = table == 'contribution_sync' or message.startswith('Contributions: ')
            if eligible and legacy_coverage_note(contribution_message):
                warnings.append(contribution_message)
            else:
                errors.append(message)
        if table == 'sync_runs':
            status = row['status']
            if status in ('partial', 'success', 'limited') and warnings:
                status = outcome(errors, warnings)
            con.execute('UPDATE sync_runs SET errors=?,warnings=?,status=? WHERE id=?',
                        (json.dumps(errors), json.dumps(warnings), status, row[key]))
        else:
            con.execute('UPDATE contribution_sync SET errors=?,warnings=? WHERE username=?',
                        (json.dumps(errors), json.dumps(warnings), row[key]))
