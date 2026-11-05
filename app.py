#!/usr/bin/env python3
"""Local PM × McKinsey tracker, using the MySQL database configured in db.md."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import unquote, urlsplit
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FIELDS = ('workstream', 'intervention', 'owner', 'edge_owner', 'status',
          'target_date', 'progress', 'blocker')
HEADERS = ('Workstream', 'Interventions', 'Intervention Owner', 'EDGE Owner',
           'Status', 'Target Delivery Date', 'Progress & Next Step',
           'Blocker / Support Needed')
STATUSES = ['Pending Confirmation', 'Not Started', 'In Progress', 'Blocked',
            'Delivered', 'On Hold / Cancelled']


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def base_path():
    """Normalize BASE_PATH to '' or '/prefix' so nginx sub-path deploys stay consistent."""
    value = os.environ.get('BASE_PATH', '').strip()
    if value in ('', '/'):
        return ''
    return '/' + value.strip('/')


def clean_record(value):
    if not isinstance(value, dict) or any(k not in (*FIELDS, 'id') for k in value):
        raise ValidationError('Invalid intervention fields.')
    record = {}
    for key in FIELDS:
        text = value.get(key, '')
        if not isinstance(text, str):
            raise ValidationError(f'{key} must be text.')
        text = text.strip()
        limit = 20000 if key in ('progress', 'blocker') else 1000
        if len(text) > limit or '\x00' in text:
            raise ValidationError(f'{key} is too long or contains invalid characters.')
        record[key] = text
    if not record['workstream'] or not record['intervention']:
        raise ValidationError('Enter an intervention and workstream.')
    if record['status'] not in STATUSES:
        raise ValidationError('Select a valid status.')
    if record['target_date']:
        try:
            if date.fromisoformat(record['target_date']).isoformat() != record['target_date']:
                raise ValueError
        except ValueError:
            raise ValidationError('Enter a valid target date in YYYY-MM-DD format.') from None
    return record


@contextmanager
def connect(db):
    if isinstance(db, dict):
        import pymysql
        conn = pymysql.connect(**db, cursorclass=pymysql.cursors.DictCursor)
    else:
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
    try:
        yield MySQLConnection(conn) if isinstance(db, dict) else conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class MySQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace('?', '%s').replace('BEGIN IMMEDIATE', 'BEGIN'), params)
        return cursor

    def executemany(self, sql, rows):
        with self.connection.cursor() as cursor:
            cursor.executemany(sql.replace('?', '%s'), rows)


def mysql_config(path=ROOT / 'db.md'):
    env_url = os.environ.get('DATABASE_URL', '').strip()
    if env_url:
        urls = [env_url]
    else:
        lines = Path(path).read_text().splitlines()
        urls = [line.split('=', 1)[1].strip().strip('\"\'')
                for line in lines if line.strip().startswith('DATABASE_URL=')]
    if len(urls) != 1:
        raise ValidationError('Set DATABASE_URL, or keep one DATABASE_URL=mysql://... line in db.md.')
    url = urlsplit(urls[0])
    if url.scheme != 'mysql' or not url.hostname or not url.username or not url.path.strip('/') or url.query:
        raise ValidationError('Expected a MySQL URL with host, username and database, without query parameters.')
    return dict(host=url.hostname, port=url.port or 3306, user=unquote(url.username),
                password=unquote(url.password or ''), database=unquote(url.path.lstrip('/')),
                charset='utf8mb4', autocommit=False, connect_timeout=10,
                read_timeout=20, write_timeout=20,
                init_command='SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')


def create_mysql_tables(db):
    tables = [
        '''CREATE TABLE IF NOT EXISTS metadata (
            `key` VARCHAR(80) PRIMARY KEY, value LONGTEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS interventions (
            id VARCHAR(80) PRIMARY KEY,
            workstream TEXT NOT NULL, intervention TEXT NOT NULL,
            owner TEXT NOT NULL, edge_owner TEXT NOT NULL,
            status TEXT NOT NULL, target_date TEXT NOT NULL,
            progress MEDIUMTEXT NOT NULL, blocker MEDIUMTEXT NOT NULL,
            source_row INTEGER, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
            position INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS saves (
            revision BIGINT PRIMARY KEY, saved_at VARCHAR(40) NOT NULL,
            changes_json LONGTEXT NOT NULL)''',
    ]
    with connect(db) as conn:
        for sql in tables:
            conn.execute(sql + ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')


def parse_workbook(source):
    """Parse the Tracker workbook into validated rows.

    Shared by the first-run SQLite seed and the local admin upload console so
    both paths enforce the same columns, blank-row rules and field validation.
    Returns (sha256 fingerprint, [(source_row, record), ...], workstream options).
    """
    source = Path(source)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    from openpyxl import load_workbook
    workbook = load_workbook(source, data_only=True, read_only=True)
    try:
        sheet = workbook['Tracker']
        rows = sheet.iter_rows(values_only=True)
        if tuple(next(rows)[:8]) != HEADERS:
            raise ValidationError('The Tracker sheet must contain the eight expected columns.')
        imported = []
        for row_number, row in enumerate(rows, 2):
            if row[1] is None or not str(row[1]).strip():
                continue
            values = dict(zip(FIELDS, [v.isoformat()[:10] if isinstance(v, (date, datetime))
                                      else str(v).strip() if v is not None else ''
                                      for v in row[:8]]))
            imported.append((row_number, clean_record(values)))
        if not imported:
            raise ValidationError('No valid interventions found. The database was not initialized.')
        options = []
        if 'Options' in workbook.sheetnames:
            options = [str(r[0]).strip() for r in workbook['Options'].iter_rows(min_row=2, values_only=True) if r[0]]
    finally:
        workbook.close()
    return fingerprint, imported, options


def initialize(db, source):
    if isinstance(db, dict):
        with connect(db) as conn:
            if not conn.execute("SELECT value FROM metadata WHERE `key`='revision'").fetchone():
                raise ValidationError('MySQL is not initialized. Run migrate_database.py first.')
        return
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (`key` TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interventions (
                id TEXT PRIMARY KEY,
                workstream TEXT NOT NULL, intervention TEXT NOT NULL,
                owner TEXT NOT NULL, edge_owner TEXT NOT NULL,
                status TEXT NOT NULL, target_date TEXT NOT NULL,
                progress TEXT NOT NULL, blocker TEXT NOT NULL,
                source_row INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saves (
                revision INTEGER PRIMARY KEY, saved_at TEXT NOT NULL,
                changes_json TEXT NOT NULL
            );
        ''')
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT value FROM metadata WHERE `key`='revision'").fetchone():
            return  # Existing browser edits must never be overwritten on restart.
        fingerprint, imported, options = parse_workbook(source)
        stamp = now()
        for position, (row_number, record) in enumerate(imported):
            conn.execute(f'INSERT INTO interventions VALUES ({",".join("?" for _ in range(13))})',
                         (str(uuid.uuid4()), *(record[k] for k in FIELDS), row_number, stamp, stamp, position))
        metadata = {'revision': 1, 'source_name': source.name, 'source_hash': fingerprint,
                    'imported_at': stamp, 'updated_at': stamp, 'imported_rows': len(imported),
                    'workstream_options': options}
        conn.executemany('INSERT INTO metadata VALUES (?, ?)',
                         [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()])
        conn.execute('INSERT INTO saves VALUES (?, ?, ?)',
                     (1, stamp, json.dumps({'type': 'excel_import', 'rows': imported}, ensure_ascii=False)))
    try:
        os.chmod(db, 0o600)
    except OSError:
        pass


def snapshot(db):
    with connect(db) as conn:
        conn.execute('BEGIN')  # Records and revision must come from the same snapshot.
        meta = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM metadata')}
        records = [dict(r) for r in conn.execute('SELECT * FROM interventions ORDER BY position')]
    return {'records': records, 'meta': meta, 'statuses': STATUSES,
            'today': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()}


def save_changes(db, payload):
    if not isinstance(payload, dict) or set(payload) != {'revision', 'changes'}:
        raise ValidationError('Invalid save request.')
    if type(payload['revision']) is not int:
        raise ValidationError('A valid revision is required.')
    changes = payload['changes']
    if not isinstance(changes, list) or not 1 <= len(changes) <= 1000:
        raise ValidationError('Save between 1 and 1,000 interventions at a time.')
    prepared, ids = [], set()
    for value in changes:
        record = clean_record(value)
        record_id = value.get('id')
        if not isinstance(record_id, str) or len(record_id) > 80 or record_id in ids:
            raise ValidationError('Invalid or duplicate intervention ID.')
        ids.add(record_id)
        prepared.append((record_id, record))
    with connect(db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        lock = ' FOR UPDATE' if isinstance(db, dict) else ''
        revision = json.loads(conn.execute("SELECT value FROM metadata WHERE `key`='revision'" + lock).fetchone()['value'])
        if payload['revision'] != revision:
            raise ConflictError('Another window updated the data. Your draft is safe. Export it before reloading.')
        stamp, audit = now(), []
        position = conn.execute('SELECT COALESCE(MAX(position), -1) AS last_position FROM interventions').fetchone()['last_position']
        for record_id, record in prepared:
            if record_id.startswith('new-'):
                position += 1
                new_id = str(uuid.uuid4())
                conn.execute(f'INSERT INTO interventions VALUES ({",".join("?" for _ in range(13))})',
                             (new_id, *(record[k] for k in FIELDS), None, stamp, stamp, position))
                audit.append({'id': new_id, 'before': None, 'after': record})
            else:
                old = conn.execute('SELECT * FROM interventions WHERE id=?', (record_id,)).fetchone()
                if old is None:
                    raise ValidationError('Intervention no longer exists. No changes were saved.')
                before = {k: old[k] for k in FIELDS}
                if before != record:
                    conn.execute(f'UPDATE interventions SET {",".join(k+"=?" for k in FIELDS)}, updated_at=? WHERE id=?',
                                 (*(record[k] for k in FIELDS), stamp, record_id))
                    audit.append({'id': record_id, 'before': before, 'after': record})
        if audit:
            revision += 1
            conn.execute('INSERT INTO saves VALUES (?, ?, ?)',
                         (revision, stamp, json.dumps(audit, ensure_ascii=False)))
            conn.execute("UPDATE metadata SET value=? WHERE `key`='revision'", (json.dumps(revision),))
            conn.execute("UPDATE metadata SET value=? WHERE `key`='updated_at'", (json.dumps(stamp),))
        # Read under the write transaction; a concurrent save cannot leak into this response.
        meta = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM metadata')}
        records = [dict(r) for r in conn.execute('SELECT * FROM interventions ORDER BY position')]
        return {'records': records, 'meta': meta, 'statuses': STATUSES,
                'today': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                'saved_count': len(audit)}


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, content_type='application/json; charset=utf-8'):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        # Behind the platform's nginx the Host header is the public domain, so compare
        # Origin against Host instead of assuming loopback, and reject cross-site calls.
        host = self.headers.get('Host')
        origin = self.headers.get('Origin')
        return (bool(host)
                and (origin is None or urlsplit(origin).netloc == host)
                and self.headers.get('Sec-Fetch-Site') != 'cross-site')

    def route(self):
        """Strip the optional BASE_PATH prefix so nginx may keep or remove it."""
        path = urlsplit(self.path).path
        prefix = getattr(self.server, 'base_path', '')
        if prefix:
            if path in (prefix, prefix + '/'):
                return '/'
            if path.startswith(prefix + '/'):
                return path[len(prefix):]
        return path

    def do_GET(self):
        if not self.trusted():
            return self.respond(403, {'error': 'Same-origin requests only.'})
        route = self.route()
        try:
            if route == '/api/data':
                return self.respond(200, snapshot(self.server.db))
            if route == '/health':
                with connect(self.server.db) as conn:
                    conn.execute('SELECT 1 FROM interventions LIMIT 1')
                storage = 'MySQL' if isinstance(self.server.db, dict) else 'SQLite'
                return self.respond(200, {'status': 'ok', 'storage': storage, 'app': 'pm-mckinsey-tracker'})
            if route == '/':
                prefix = getattr(self.server, 'base_path', '')
                html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
                return self.respond(200, html.replace('__BASE_PATH__', prefix), 'text/html; charset=utf-8')
            files = {'/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
                     '/assets/publicis.png': ('assets/publicis.png', 'image/png'),
                     '/assets/edge.png': ('assets/edge.png', 'image/png'),
                     '/assets/edge-dark.png': ('assets/edge-dark.png', 'image/png')}
            if route not in files:
                return self.respond(404, {'error': 'Not found'})
            filename, mime = files[route]
            return self.respond(200, (ROOT / 'static' / filename).read_bytes(), mime)
        except Exception:
            logging.exception('Read failed')
            return self.respond(500, {'error': 'Unable to load data. Check the local server.'})

    def do_POST(self):
        if not self.trusted():
            return self.respond(403, {'error': 'Same-origin requests only.'})
        if self.route() != '/api/save':
            return self.respond(404, {'error': 'Not found'})
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.respond(415, {'error': 'Only JSON requests are accepted.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4_000_000:
                return self.respond(413, {'error': 'Save request is empty or exceeds 4 MB.'})
            payload = json.loads(self.rfile.read(size))
            return self.respond(200, save_changes(self.server.db, payload))
        except ConflictError as exc:
            return self.respond(409, {'error': str(exc)})
        except (ValidationError, ValueError, UnicodeError) as exc:
            return self.respond(400, {'error': str(exc) if isinstance(exc, ValidationError) else 'Invalid save data.'})
        except Exception:
            logging.exception('Save failed; transaction rolled back')
            return self.respond(500, {'error': 'Save failed. No changes were committed. Your draft is safe; please retry.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT') or 9733))
    parser.add_argument('--host', default='127.0.0.1', help='Bind address; the container uses 0.0.0.0.')
    parser.add_argument('--db', type=Path, help='Use an explicit SQLite file (tests or local recovery).')
    parser.add_argument('--source', type=Path, default=ROOT / 'EDGE_AI_Transformation_Tracker.xlsx')
    args = parser.parse_args()
    db = args.db if args.db is not None else mysql_config()
    initialize(db, args.source)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.db = db
    server.base_path = base_path()
    storage = 'MySQL (db.md)' if isinstance(db, dict) else f'SQLite: {db}'
    print(f'PM × McKinsey | http://{args.host}:{server.server_port}{server.base_path or "/"} | {storage}', flush=True)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
