#!/usr/bin/env python3
"""Online MySQL publishing for admin-imported Tracker workbooks.

The hosted dashboard (app.py) stays read-only and keeps serving the
interventions/metadata/saves tables. This module is the only writer of those
tables outside the browser save flow: an admin confirms an uploaded workbook,
and publish_snapshot replaces the whole dataset inside one MySQL transaction.
"""
import json
import logging
import os

import app


def configured():
    return bool(os.environ.get('DATABASE_URL', '').strip())


def remote_snapshot():
    """Read the live dashboard tables into the same snapshot shape used locally."""
    db = app.mysql_config()
    with app.connect(db) as conn:
        if not conn.execute("SELECT 1 FROM metadata WHERE `key`='revision'").fetchone():
            raise LookupError('Online database is not initialized yet.')
        meta = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM metadata')}
        records = [dict(r) for r in conn.execute('SELECT * FROM interventions ORDER BY position')]
    return {'records': records, 'meta': meta}


def publish_snapshot(snapshot, source_hash):
    """Replace the online dataset with an admin-confirmed workbook snapshot.

    Returns the new revision number. Runs under a named MySQL lock so two admins
    cannot interleave publications, and rolls back fully on any failure.
    """
    records = snapshot['records']
    meta = snapshot['meta']
    db = app.mysql_config()
    with app.connect(db) as conn:
        lock = conn.execute("SELECT GET_LOCK('pm_tracker_import', 15)").fetchone()
        if list(lock.values())[0] != 1:
            raise TimeoutError('Another publication is running; retry in a moment.')
        try:
            row = conn.execute("SELECT value FROM metadata WHERE `key`='revision' FOR UPDATE").fetchone()
            if not row:
                raise LookupError('Online database is not initialized yet.')
            revision = json.loads(row['value']) + 1
            stamp = app.now()
            conn.execute('DELETE FROM interventions')
            for record in records:
                conn.execute(
                    f'INSERT INTO interventions VALUES ({",".join("?" for _ in range(13))})',
                    (record['id'], *(record[k] for k in app.FIELDS),
                     record.get('source_row'), stamp, stamp, record['position']))
            new_meta = {
                'revision': revision,
                'source_name': meta['source_name'],
                'source_hash': source_hash,
                'imported_at': stamp,
                'updated_at': stamp,
                'imported_rows': len(records),
                'workstream_options': meta.get('workstream_options', []),
                'published_by': 'local-admin',
            }
            conn.execute("DELETE FROM metadata WHERE `key` IN ('source_name','source_hash','imported_at','updated_at','imported_rows','workstream_options','published_by')")
            conn.execute("UPDATE metadata SET value=? WHERE `key`='revision'", (json.dumps(revision),))
            conn.executemany('INSERT INTO metadata VALUES (?, ?)',
                             [(k, json.dumps(v, ensure_ascii=False)) for k, v in new_meta.items() if k != 'revision'])
            audit = {'type': 'admin_workbook_publish', 'source': meta['source_name'],
                     'rows': len(records), 'sha256': source_hash}
            conn.execute('INSERT INTO saves VALUES (?, ?, ?)',
                         (revision, stamp, json.dumps(audit, ensure_ascii=False)))
            return revision
        except Exception:
            logging.exception('Online publish failed; transaction rolled back')
            raise
        finally:
            conn.execute("SELECT RELEASE_LOCK('pm_tracker_import')")
