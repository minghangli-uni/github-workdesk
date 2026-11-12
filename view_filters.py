"""Validation for saved view filters; work records and unsaved editors are separate."""
from datetime import date

VIEWS = {'overview', 'reviews', 'prs', 'issues', 'tasks', 'completed'}
ROLES = {'all', 'author', 'assigned', 'review_requested', 'reviewer', 'coauthor', 'code',
         'committer', 'commenter', 'personal', 'review_previous', 'unassigned'}
STATUS = {'any', 'outstanding', 'finished', 'open', 'draft', 'merged', 'closed_unmerged',
          'closed_issue', 'closed_unknown', 'todo', 'in_progress', 'blocked', 'done', 'snoozed', 'committed'}
TABLE = {'period': {'all','7','30','90','365','custom'}, 'dateField': {'updated_at','created_at','closed_at'},
         'role': ROLES, 'status': STATUS, 'repo': 200, 'query': 300, 'from': 'date', 'to': 'date'}
CONTRIBUTIONS = {'tab': {'active','history'}, 'repo': 200, 'role': ROLES,
                 'status': {'any','open','draft','merged','closed_unmerged','closed_unknown','committed'},
                 'period': {'all','7','30','90','365'}, 'query': 300, 'discussion': bool, 'sort': {'activity','mine'}}


def validate(value, schema):
    if not isinstance(value, dict) or set(value) - set(schema):
        raise ValueError('Invalid saved filters.')
    for key, item in value.items():
        rule = schema[key]
        if rule is bool:
            valid = type(item) is bool
        elif isinstance(rule, int):
            valid = isinstance(item, str) and len(item) <= rule
        elif isinstance(rule, set):
            valid = isinstance(item, str) and item in rule
        else:
            valid = isinstance(item, str)
            if valid and item:
                try:
                    valid = date.fromisoformat(item).isoformat() == item
                except ValueError:
                    valid = False
        if not valid:
            raise ValueError(f'Invalid saved filter: {key}.')
    return value.copy()


def validate_saved(value):
    if not isinstance(value, dict) or set(value) - {'tables', 'contributions'}:
        raise ValueError('Invalid saved filters.')
    tables = value.get('tables', {})
    if not isinstance(tables, dict) or set(tables) - VIEWS:
        raise ValueError('Invalid saved work view.')
    return {'tables': {key: validate(filters, TABLE) for key, filters in tables.items()},
            'contributions': validate(value.get('contributions', {}), CONTRIBUTIONS)}
