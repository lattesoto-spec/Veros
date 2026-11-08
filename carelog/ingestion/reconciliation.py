"""Explicit review when clocked work disagrees with another duration column."""
import hashlib
import json

from .durations import duration_minutes, needs_unit_review
from .fingerprint import normalize_header
from .quality import _minutes


def _candidates(sheet, target):
    fields = target.get('fields', {})
    mapped = fields.get('minutes')
    candidates = {}
    for header in sheet.headers:
        name = normalize_header(header)
        tokens = set(name.split('_'))
        if tokens & {'break', 'breaks', 'unpaid', 'leave', 'cost', 'rate', 'contract', 'contracted', 'weekly'}:
            continue
        duration_label = bool(tokens & {'paid', 'worked', 'actual', 'total', 'net', 'duration', 'shift'})
        if name in {'hours', 'hrs'} or (duration_label and tokens & {'hours', 'hrs'}):
            candidates[header] = 'hours'
        elif name in {'minutes', 'mins'} or (duration_label and tokens & {'minutes', 'mins'}):
            candidates[header] = 'minutes'
    if mapped and mapped.get('column') and not needs_unit_review(mapped):
        candidates[mapped['column']] = mapped.get('unit') or ('hours' if mapped.get('multiply') == 60 else 'minutes')
    return candidates


def duration_holds(records, target, sheet, filename):
    if not {'start_time', 'end_time'} <= set(target.get('fields', {})):
        return []
    raw_rows = {sheet.source_rows[i] if sheet.source_rows else sheet.header_row_index + 2 + i: row
                for i, row in enumerate(sheet.rows)}
    holds = []
    for column, unit in _candidates(sheet, target).items():
        mismatches = []
        for record in records:
            if not record.get('start_time') or not record.get('end_time'):
                continue
            raw = str(raw_rows.get(record.get('_source_row'), {}).get(column) or '').strip()
            try:
                reported = duration_minutes(raw, unit)
            except ValueError:
                reported = None
            clocked = _minutes(record)
            if reported is None or abs(reported - clocked) > 1:
                mismatches.append({'row': record.get('_source_row'), 'date': str(record['date']), 'staff_id': record.get('staff_id'),
                    'source_value': raw, 'source_unit': unit, 'reported_minutes': reported,
                    'clock_minutes': clocked, 'start': str(record.get('start_time')),
                    'finish': str(record.get('end_time')), 'break_minutes': record.get('break_minutes'),
                    'timezone': record.get('_timezone_name')})
        if not mismatches:
            continue
        token = hashlib.sha256(json.dumps(mismatches, sort_keys=True).encode()).hexdigest()
        decision = target.get('duration_reconciliation', {}).get(column, {})
        if decision.get('fingerprint') == token and decision.get('basis'):
            continue
        holds.append({'kind': 'time_reconciliation', 'file': filename, 'sheet': sheet.name,
            'target_kind': 'shifts', 'field': column, 'column': column,
            'options': ['clocks_verified', 'paid_units'], 'fingerprint': token,
            'rows': len(mismatches), 'sample': mismatches[:10]})
    return holds
