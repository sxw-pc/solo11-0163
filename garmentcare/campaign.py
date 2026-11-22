"""Offline intake of synthetic laundry campaign visits, bookings and signed evidence.

Every row is an independently signed (offline evidence label) event. Raw rows are
journaled through :mod:`garmentcare.source`; derived attribution lives in
:mod:`garmentcare.attribution` and is fully recomputable from these events.
"""

import json

from .source import record

# Each tuple: (json key, table, column list). States and reasons on
# qr_coupon_redeem / login_commitment / referral_credential are derived columns;
# importers store placeholders that attribution.reevaluate overwrites.
EVIDENCE_TABLES = [
    ('touches', 'campaign_touch',
     ('event_id', 'visitor', 'campaign', 'channel', 'at')),
    ('bookings', 'service_booking',
     ('event_id', 'visitor', 'garment_id', 'at')),
    ('coupon_issues', 'qr_coupon_issue',
     ('event_id', 'coupon_no', 'campaign', 'shop', 'visitor', 'issued_at')),
    ('coupon_redeems', 'qr_coupon_redeem',
     ('event_id', 'coupon_no', 'campaign', 'shop', 'visit_id', 'at',
      'customer_signer', 'clerk_signer', 'referral_event_id')),
    ('referrals', 'referral_credential',
     ('event_id', 'referrer', 'referee', 'campaign', 'at',
      'referrer_signer', 'referee_signer', 'clerk_signer')),
    ('shared_device_declarations', 'shared_device_declaration',
     ('event_id', 'visitor', 'users', 'declarant', 'at')),
    ('identity_links', 'identity_link',
     ('event_id', 'visitor', 'customer', 'kind', 'account', 'at')),
    ('consent_withdrawals', 'consent_withdrawal',
     ('event_id', 'visitor', 'at')),
    ('login_commitments', 'login_commitment',
     ('event_id', 'customer', 'campaign', 'session_id', 'account', 'at', 'signer')),
    ('store_visits', 'store_visit',
     ('event_id', 'customer', 'shop', 'at', 'garment_id', 'status',
      'customer_signer', 'clerk_signer')),
]

DERIVED_DEFAULTS = {
    'coupon_redeems': ('pending_review', 'awaiting attribution reevaluation'),
    'login_commitments': ('pending_review', 'awaiting attribution reevaluation'),
    'referrals': ('pending_review', 'awaiting attribution reevaluation'),
}


def _insert(db, key, table, columns, row):
    if not record(db, row['event_id'], table, row):
        return
    values = [row.get(col) for col in columns]
    if key in DERIVED_DEFAULTS:
        values.extend(DERIVED_DEFAULTS[key])
    elif key == 'shared_device_declarations':
        # users is a list of household members sharing the device
        values[2] = json.dumps(row['users'], ensure_ascii=False, sort_keys=True)
    placeholders = ','.join('?' for _ in values)
    db.execute(f'INSERT INTO {table} VALUES({placeholders})', values)


def import_data(db, data):
    with db:
        for key, table, columns in EVIDENCE_TABLES:
            for row in data.get(key, []):
                _insert(db, key, table, columns, row)
        from .attribution import reevaluate
        decisions = reevaluate(db)
    return decisions
