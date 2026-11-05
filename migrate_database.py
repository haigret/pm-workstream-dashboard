#!/usr/bin/env python3
"""One-shot copy of the local SQLite tracker into the MySQL database from db.md."""
import argparse
import sqlite3
from pathlib import Path

import app

INTERVENTION_COLUMNS = ('id', 'workstream', 'intervention', 'owner', 'edge_owner', 'status',
                        'target_date', 'progress', 'blocker', 'source_row',
                        'created_at', 'updated_at', 'position')


def read_sqlite(path):
    if not Path(path).exists():
        raise SystemExit(f'Source SQLite file not found: {path}')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {'metadata', 'interventions', 'saves'} - tables
        if missing:
            raise SystemExit(f'{path} is not a tracker database (missing {sorted(missing)}).')
        return (list(conn.execute('SELECT * FROM metadata')),
                list(conn.execute('SELECT * FROM interventions ORDER BY position')),
                list(conn.execute('SELECT * FROM saves ORDER BY revision')))
    finally:
        conn.close()


def migrate(source):
    metadata, interventions, saves = read_sqlite(source)
    if not interventions:
        raise SystemExit(f'{source} holds no interventions; nothing to migrate.')
    app.create_mysql_tables(app.mysql_config())
    with app.connect(app.mysql_config()) as conn:
        # ponytail: a plain existence check is enough here; the whole copy runs in one
        # transaction, so a crash leaves MySQL untouched and the script can be rerun.
        if conn.execute("SELECT value FROM metadata WHERE `key`='revision'").fetchone():
            raise SystemExit('MySQL already holds tracker data. Refusing to overwrite it.\n'
                             'Drop the metadata/interventions/saves tables first if this is intentional.')
        conn.executemany(
            'INSERT INTO interventions ({}) VALUES ({})'.format(
                ', '.join(INTERVENTION_COLUMNS), ', '.join(['?'] * len(INTERVENTION_COLUMNS))),
            [tuple(row[c] for c in INTERVENTION_COLUMNS) for row in interventions])
        conn.executemany('INSERT INTO metadata (`key`, value) VALUES (?, ?)',
                         [(row['key'], row['value']) for row in metadata])
        conn.executemany('INSERT INTO saves (revision, saved_at, changes_json) VALUES (?, ?, ?)',
                         [(row['revision'], row['saved_at'], row['changes_json']) for row in saves])
        got = (conn.execute('SELECT COUNT(*) AS n FROM metadata').fetchone()['n'],
               conn.execute('SELECT COUNT(*) AS n FROM interventions').fetchone()['n'],
               conn.execute('SELECT COUNT(*) AS n FROM saves').fetchone()['n'])
    expected = (len(metadata), len(interventions), len(saves))
    if got != expected:
        raise SystemExit(f'Verification failed: MySQL has {got} rows, expected {expected}.')
    print(f'Migrated {source} -> MySQL (db.md)')
    print(f'  metadata      {got[0]} rows')
    print(f'  interventions {got[1]} rows')
    print(f'  saves         {got[2]} rows')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='source', type=Path,
                        default=app.ROOT / 'data' / 'tracker.sqlite3',
                        help='SQLite file to copy from (default: data/tracker.sqlite3).')
    migrate(parser.parse_args().source)


if __name__ == '__main__':
    main()
