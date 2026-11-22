import json

from . import catalog
from .source import record
from .store import update


def _garment(db, garment_id):
    return db.execute('SELECT * FROM garment WHERE id=?', (garment_id,)).fetchone()


def decide(db, garment_id):
    garment = _garment(db, garment_id)
    if not garment:
        return None
    photos = db.execute('SELECT * FROM intake_photo WHERE garment_id=? ORDER BY version DESC,event_id',
                        (garment_id,)).fetchall()
    if not photos:
        state, reason, photo_id = 'pending', 'signed intake photo missing', None
    else:
        photo = photos[0]
        same = [p for p in photos if p['version'] == photo['version']]
        if len({(p['material'], p['damage'], p['photo_sha']) for p in same}) > 1:
            state, reason, photo_id = 'frozen', 'parallel intake image version conflict', None
        elif photo['material'] != garment['material']:
            state, reason, photo_id = 'frozen', 'material label conflicts with signed intake', None
        elif photo['owner_signer'] != garment['owner'] or photo['clerk_signer'] == garment['owner']:
            state, reason, photo_id = 'pending', 'independent signed baseline missing', None
        elif garment['allowed_process'] == '':
            state, reason, photo_id = 'pending', 'owner has not authorized a care process', None
        else:
            state, reason, photo_id = 'accepted', 'material, baseline and care consent verified', photo['event_id']

    review = catalog.garment_label(db, garment)
    label_state = review['label_state'] if review else 'not_tracked'
    label_material = review['label_material'] if review else None
    label_care = review['label_care'] if review else None
    label_event = review['label_event'] if review else None

    # The signed intake photo is immutable evidence.  A catalogue or tag
    # contradiction opens a review; it never rewrites the signed baseline.
    if state == 'accepted' and review:
        if review['impact'] == 'frozen_intake':
            state, reason = 'frozen', review['reason']
        elif label_state == 'conflict':
            reason = review['reason']
        elif label_state == 'verified' and review['task_state'] == 'none':
            reason = 'material, baseline, care consent and label authority verified'
        elif label_state in ('pending', 'withdrawn'):
            # Unconfirmed or withdrawn material remains pending evidence and
            # leaves the signed intake decision standing.
            pass

    result = update(db, 'intake_decision', garment_id, state=state, reason=reason, photo_id=photo_id,
                    label_material=label_material, label_care=label_care, label_status=label_state,
                    label_event=label_event, _evidence_ref=label_event)
    from .process import decide as process_decide
    process_decide(db, garment_id)
    return result


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('garments', []):
            if record(db, row['event_id'], 'garment', row):
                db.execute(
                    'INSERT INTO garment(id,owner,material,accepted_at,declared_value,cap_rate,allowed_process,'
                    'pickup_person,claim_until,expected_bag,sku,label_route,label_route_basis) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['id'], row['owner'], row['material'], row['accepted_at'], row['declared_value'],
                     row['cap_rate'], row['allowed_process'], row['pickup_person'], row['claim_until'],
                     row['expected_bag'], row.get('sku', ''), row.get('label_route', 'manufacturer'),
                     row.get('label_route_basis', '')),
                )
            else:
                # A later envelope may only add the catalogue link / authority
                # route; it may not rewrite the originally signed facts.
                current = _garment(db, row['id'])
                if current:
                    sku = row.get('sku', current['sku'])
                    route = row.get('label_route', current['label_route'])
                    basis = row.get('label_route_basis', current['label_route_basis'])
                    if (sku, route, basis) != (current['sku'], current['label_route'], current['label_route_basis']):
                        db.execute('UPDATE garment SET sku=?,label_route=?,label_route_basis=? WHERE id=?',
                                   (sku, route, basis, row['id']))
            touched.add(row['id'])
        for row in data.get('photos', []):
            if record(db, row['event_id'], 'intake_photo', row):
                db.execute('INSERT INTO intake_photo VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['garment_id'], row['version'], row['photo_sha'],
                            json.dumps(row['damage'], sort_keys=True), row['owner_signer'],
                            row['clerk_signer'], row['material']))
            touched.add(row['garment_id'])
        return [decide(db, g) for g in sorted(touched)]
