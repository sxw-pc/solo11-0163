import json

from .attribution import campaign_totals


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        garments.append(item)
    acquisition=[dict(row) for row in db.execute('SELECT * FROM acquisition_decision ORDER BY booking_id')]
    customers=[dict(row) for row in db.execute('SELECT * FROM customer_acquisition ORDER BY customer')]
    touch_roles=[dict(row) for row in db.execute('SELECT * FROM touch_classification ORDER BY event_id')]
    coupons={
        'issues':[dict(row) for row in db.execute('SELECT * FROM qr_coupon_issue ORDER BY event_id')],
        'redeems':[dict(row) for row in db.execute('SELECT * FROM qr_coupon_redeem ORDER BY event_id')],
    }
    referrals=[dict(row) for row in db.execute('SELECT * FROM referral_credential ORDER BY event_id')]
    visits=[dict(row) for row in db.execute('SELECT * FROM store_visit ORDER BY event_id')]
    return {'garments':garments,
            'acquisition':acquisition,
            'customer_acquisition':customers,
            'campaign_totals':campaign_totals(db),
            'touch_classification':touch_roles,
            'coupons':coupons,
            'referrals':referrals,
            'store_visits':visits,
            'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
