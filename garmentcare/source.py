"""Evidence storage and deterministic content digests.

Two different questions are kept apart here:

* the *event* digest identifies one collected envelope (``record``);
* the *content* digest identifies the bytes of a published page, so two
  fetches of the same page are recognisable as one publication even when
  their event ids or fetch timestamps differ.
"""

import hashlib
import json


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256(raw):
    if isinstance(raw, str):
        raw = raw.encode()
    return hashlib.sha256(raw).hexdigest()


def record(db, event_id, kind, value):
    body = canonical_json(value)
    sha = hashlib.sha256(body.encode()).hexdigest()
    old = db.execute('SELECT kind,sha FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        if (old['kind'], old['sha']) != (kind, sha):
            raise ValueError(f'event_id conflict: {event_id}')
        return False
    db.execute('INSERT INTO evidence(event_id,kind,sha,body) VALUES(?,?,?,?)', (event_id, kind, sha, body))
    return True


def file_record(db, kind, raw):
    sha = hashlib.sha256(raw).hexdigest()
    if not db.execute('SELECT 1 FROM file_journal WHERE kind=? AND sha=?', (kind, sha)).fetchone():
        db.execute('INSERT INTO file_journal(kind,sha,raw) VALUES(?,?,?)', (kind, sha, raw))
    return sha
