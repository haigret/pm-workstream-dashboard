#!/usr/bin/env python3
"""Loopback-only Excel upload console for the PM Workstream Dashboard.

The hosted app.py stays read-only over HTTP. This tool runs on the operator's
machine, parses an uploaded Tracker workbook into a preview, stores confirmed
imports in a local SQLite history (plus a browsable tracker.sqlite3), and only
publishes to the online MySQL after an explicit second confirmation.
"""
import argparse
import hashlib
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
import webbrowser
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

import app
import mysql_store

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024
PREVIEW_TTL_SECONDS = 1800


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def version(snapshot):
    """Content fingerprint of a snapshot, stable across restarts and re-uploads."""
    if not snapshot:
        return ''
    payload = json.dumps(
        [{k: row.get(k) for k in (*app.FIELDS, 'source_row', 'position')} for row in snapshot['records']],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def key_of(row):
    """Excel row number is the stable identity for workbook-driven imports."""
    return row['source_row'] if row.get('source_row') is not None else row['id']


def difference(before, after):
    old = {key_of(r): r for r in before['records']} if before else {}
    new = {key_of(r): r for r in after['records']}
    comparable = lambda r: {k: r[k] for k in app.FIELDS}
    changed = sum(comparable(old[k]) != comparable(new[k]) for k in old.keys() & new.keys())
    return dict(added=len(new.keys() - old.keys()), removed=len(old.keys() - new.keys()),
                changed=changed, unchanged=len(old.keys() & new.keys()) - changed)


def build_snapshot(imported, options, source_name, fingerprint):
    stamp = app.now()
    records = []
    for position, (row_number, record) in enumerate(imported):
        records.append({'id': str(uuid.uuid4()),
                        **{k: record[k] for k in app.FIELDS},
                        'source_row': row_number, 'created_at': stamp,
                        'updated_at': stamp, 'position': position})
    meta = {'revision': 1, 'source_name': source_name, 'source_hash': fingerprint,
            'imported_at': stamp, 'updated_at': stamp, 'imported_rows': len(records),
            'workstream_options': options}
    return {'records': records, 'meta': meta}


class AdminState:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / 'history.sqlite3'
        self.browse_db = self.directory / 'tracker.sqlite3'
        self.pending = None
        self.lock = threading.RLock()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS imports ('
                       'id INTEGER PRIMARY KEY, imported_at TEXT NOT NULL, source TEXT NOT NULL, '
                       'sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS publications ('
                       'id INTEGER PRIMARY KEY, published_at TEXT NOT NULL, '
                       'local_version TEXT NOT NULL, source TEXT NOT NULL)')

    def latest(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute('SELECT snapshot_json FROM imports ORDER BY id DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def status(self):
        with self.lock, closing(sqlite3.connect(self.database)) as db, db:
            current = self.latest()
            history = [dict(id=row_id, importedAt=imported_at, source=source, sha256=sha)
                       for row_id, imported_at, source, sha in db.execute(
                           'SELECT id, imported_at, source, sha256 FROM imports ORDER BY id DESC LIMIT 20')]
            published = db.execute('SELECT published_at, local_version, source FROM publications '
                                   'ORDER BY id DESC LIMIT 1').fetchone()
        publication = dict(publishedAt=published[0], localVersion=published[1], source=published[2]) if published else None
        return dict(current=current['meta'] if current else None,
                    localVersion=version(current), history=history,
                    onlineConfigured=mysql_store.configured(), publication=publication)

    def preview(self, data, filename):
        filename = unquote(filename).replace('\\', '/').rsplit('/', 1)[-1].strip()
        if not filename.lower().endswith('.xlsx') or len(filename) > 180 or any(ord(c) < 32 for c in filename):
            raise RequestError('请选择 .xlsx 文件（不支持 .xls、.xlsm 或加密文件）。')
        if not data or len(data) > MAX_UPLOAD:
            raise RequestError('文件为空或超过 20 MB。', 413)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 2000 or sum(m.file_size for m in members) > MAX_EXPANDED:
                    raise RequestError('Excel 解压后过大，请缩小文件后重试。', 413)
                if any(m.flag_bits & 1 for m in members):
                    raise RequestError('请先移除 Excel 文件密码。')
                if '[Content_Types].xml' not in archive.namelist() or 'xl/workbook.xml' not in archive.namelist():
                    raise RequestError('文件不是有效的 Excel 工作簿。')
            with TemporaryDirectory(prefix='pm-admin-preview-') as directory:
                source = Path(directory) / filename
                source.write_bytes(data)
                fingerprint, imported, options = app.parse_workbook(source)
        except RequestError:
            raise
        except Exception:
            logging.warning('Workbook validation failed', exc_info=True)
            raise RequestError('无法读取该工作簿。请使用含 Tracker 工作表和八个标准列的模板，检查表头与文件密码。') from None
        snapshot = build_snapshot(imported, options, filename, fingerprint)
        with self.lock:
            current = self.latest()
            token = secrets.token_urlsafe(24)
            self.pending = dict(token=token, snapshot=snapshot, data=data, sha256=fingerprint,
                                base_version=version(current), expires=time.monotonic() + PREVIEW_TTL_SECONDS)
            return dict(previewId=token, snapshot=snapshot, diff=difference(current, snapshot),
                        baseline=current['meta'] if current else None,
                        identical=bool(current and current['meta'].get('source_hash') == fingerprint
                                       and current['meta'].get('source_name') == filename))

    def commit(self, token):
        with self.lock:
            pending = self.pending
            if not pending or not secrets.compare_digest(str(token), pending['token']) \
                    or time.monotonic() > pending['expires']:
                raise RequestError('预览已失效，请重新上传文件。', 409)
            if version(self.latest()) != pending['base_version']:
                raise RequestError('本地数据已被另一窗口更新，请重新上传并核对预览。', 409)
            uploads = self.directory / 'uploads'
            uploads.mkdir(exist_ok=True, mode=0o700)
            archive = uploads / (pending['sha256'] + '.xlsx')
            if not archive.exists():
                temporary = archive.with_suffix('.tmp')
                temporary.write_bytes(pending['data'])
                temporary.replace(archive)
            snapshot = pending['snapshot']
            previous = self.latest()
            diff = difference(previous, snapshot) if previous else dict(
                added=len(snapshot['records']), removed=0, changed=0, unchanged=0)
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('INSERT INTO imports (imported_at, source, sha256, snapshot_json) VALUES (?, ?, ?, ?)',
                           (app.now(), snapshot['meta']['source_name'], pending['sha256'],
                            json.dumps(snapshot, ensure_ascii=False)))
            self.export_sqlite(snapshot)
            self.pending = None
            changed = bool(diff['added'] or diff['removed'] or diff['changed'])
            return dict(changed=changed, meta=snapshot['meta'], diff=diff)

    def export_sqlite(self, snapshot):
        """Write a browsable copy that `python app.py --db <file>` can serve."""
        with closing(sqlite3.connect(self.browse_db)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (`key` TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS interventions (
                    id TEXT PRIMARY KEY, workstream TEXT NOT NULL, intervention TEXT NOT NULL,
                    owner TEXT NOT NULL, edge_owner TEXT NOT NULL, status TEXT NOT NULL,
                    target_date TEXT NOT NULL, progress TEXT NOT NULL, blocker TEXT NOT NULL,
                    source_row INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    position INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS saves (
                    revision INTEGER PRIMARY KEY, saved_at TEXT NOT NULL, changes_json TEXT NOT NULL);
            ''')
            db.execute('DELETE FROM interventions')
            db.execute('DELETE FROM metadata')
            db.execute('DELETE FROM saves')
            for row in snapshot['records']:
                db.execute(f'INSERT INTO interventions VALUES ({",".join("?" for _ in range(13))})',
                           (row['id'], *(row[k] for k in app.FIELDS), row['source_row'],
                            row['created_at'], row['updated_at'], row['position']))
            db.executemany('INSERT INTO metadata VALUES (?, ?)',
                           [(k, json.dumps(v, ensure_ascii=False)) for k, v in snapshot['meta'].items()])
            db.execute('INSERT INTO saves VALUES (?, ?, ?)',
                       (1, snapshot['meta']['imported_at'],
                        json.dumps({'type': 'admin_local_import', 'source': snapshot['meta']['source_name']},
                                   ensure_ascii=False)))
        try:
            os.chmod(self.browse_db, 0o600)
        except OSError:
            pass

    def online(self):
        with self.lock:
            local = self.latest()
            if not mysql_store.configured():
                raise RequestError('未配置线上数据库。请在项目 .env 中配置 DATABASE_URL；不要在聊天中发送密码。', 503)
            try:
                remote = mysql_store.remote_snapshot()
            except LookupError:
                remote = None
            except Exception:
                logging.warning('Online check unavailable', exc_info=True)
                raise RequestError('无法连接线上 MySQL，本地数据不受影响。请检查网络、数据库授权和 .env 配置。', 503) from None
            return dict(current=remote['meta'] if remote else None,
                        remoteVersion=version(remote), localVersion=version(local),
                        diff=difference(remote, local) if local and remote else None)

    def publish(self, expected_local, expected_remote):
        with self.lock:
            snapshot = self.latest()
            if not snapshot or version(snapshot) != expected_local:
                raise RequestError('本地快照已变化，请重新检查后发布。', 409)
            online = self.online()
            if online['remoteVersion'] != expected_remote:
                raise RequestError('线上数据已变化，请重新检查后发布。', 409)
            try:
                revision = mysql_store.publish_snapshot(snapshot, snapshot['meta']['source_hash'])
            except Exception:
                logging.warning('Online publish failed', exc_info=True)
                raise RequestError('发布未确认成功，本地数据已保留。请检查线上连接后重试。', 503) from None
            stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('INSERT INTO publications (published_at, local_version, source) VALUES (?, ?, ?)',
                           (stamp, version(snapshot), snapshot['meta']['source_name']))
            return dict(revision=revision, meta=snapshot['meta'])


def make_server(state, port=8766):
    csrf_token = secrets.token_urlsafe(32)

    class Handler(app.Handler):
        def send(self, status, data, mime='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            for key, value in [('Content-Type', mime), ('Content-Length', str(len(body))),
                               ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                               ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'no-referrer'),
                               ('Content-Security-Policy',
                                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")]:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def validate(self, writing=False):
            # Loopback binding alone does not stop DNS rebinding or browser CSRF.
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            if host not in allowed or (origin and origin != 'http://' + host):
                raise RequestError('仅允许从本机后台页面访问。', 403)
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise RequestError('不允许跨站请求。', 403)
            if writing and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf_token):
                raise RequestError('页面会话已失效，请刷新后台后重试。', 403)

        def do_GET(self):
            try:
                route = urlsplit(self.path).path
                if route == '/dashboard' or route.startswith('/dashboard/'):
                    if not state.latest():
                        raise RequestError('请先在后台上传并确认导入 Excel。', 404)
                    self.validate()
                    # Reuse the hosted dashboard handler against the browsable SQLite copy.
                    self.server.db = state.browse_db
                    self.server.base_path = '/dashboard'
                    return app.Handler.do_GET(self)
                self.validate()
                if route in ('/', '/admin'):
                    page = (ROOT / 'local-admin' / 'index.html').read_text(encoding='utf-8')
                    self.send(200, page.replace('__CSRF_TOKEN__', csrf_token).encode(), 'text/html; charset=utf-8')
                elif route == '/api/status':
                    self.send(200, state.status())
                elif route == '/api/data':
                    snapshot = state.latest()
                    if not snapshot:
                        raise RequestError('请先在后台上传并确认导入 Excel。', 404)
                    self.send(200, dict(snapshot, statuses=app.STATUSES,
                                        today=datetime.now(app.ZoneInfo('Asia/Shanghai')).date().isoformat()))
                elif route == '/favicon.ico':
                    self.send(204, b'')
                else:
                    raise RequestError('页面不存在。', 404)
            except RequestError as error:
                self.send(error.status, dict(error=str(error)))
            except Exception:
                logging.exception('Local read failed')
                self.send(500, dict(error='本地数据读取失败，请查看后台终端。'))

        def do_POST(self):
            route = urlsplit(self.path).path
            # Dashboard saves target the local browsable copy; same-origin is enforced
            # by app.Handler itself, and the admin CSRF token only guards admin APIs.
            if route.startswith('/dashboard/api/'):
                if not state.latest():
                    return self.send(404, dict(error='请先在后台上传并确认导入 Excel。'))
                self.validate()
                self.server.db = state.browse_db
                self.server.base_path = '/dashboard'
                return app.Handler.do_POST(self)
            try:
                self.validate(writing=True)
                if route not in ('/api/preview', '/api/import', '/api/online', '/api/publish'):
                    raise RequestError('接口不存在。', 404)
                if self.headers.get('Transfer-Encoding'):
                    raise RequestError('不支持流式上传。')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise RequestError('无效的文件长度。')
                limit = MAX_UPLOAD if route == '/api/preview' else 4096
                if length <= 0 or length > limit:
                    raise RequestError('请求为空或超过大小限制（Excel 最大 20 MB）。', 413)
                self.connection.settimeout(30)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise RequestError('上传中断，请重试。')
                if route == '/api/preview':
                    if self.headers.get_content_type() != 'application/octet-stream':
                        raise RequestError('请通过文件上传控件提交 Excel。')
                    result = state.preview(body, self.headers.get('X-File-Name', ''))
                else:
                    if self.headers.get_content_type() != 'application/json':
                        raise RequestError('请求格式不正确。')
                    try:
                        payload = json.loads(body)
                        if not isinstance(payload, dict):
                            raise ValueError
                    except (ValueError, UnicodeDecodeError):
                        raise RequestError('请求内容不是有效 JSON。')
                    if route == '/api/import':
                        result = state.commit(payload.get('previewId', ''))
                    elif route == '/api/online':
                        result = state.online()
                    else:
                        result = state.publish(payload.get('localVersion', ''), payload.get('remoteVersion', ''))
                self.send(200, result)
            except RequestError as error:
                self.send(error.status, dict(error=str(error)))
            except (TimeoutError, ConnectionError):
                self.close_connection = True
            except Exception:
                logging.exception('Local operation failed')
                self.send(500, dict(error='操作失败。已有快照未被空数据覆盖，请检查磁盘空间或重试。'))

        def log_message(self, fmt, *args):
            if args and str(args[0]).startswith(('GET /api/', 'POST /api/')):
                return
            super().log_message(fmt, *args)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def load_dotenv(path=ROOT / '.env'):
    """Load KEY=VALUE pairs from a local .env without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip().strip('"\'')
        if key and key not in os.environ:
            os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'local-admin-data')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    load_dotenv()
    server = make_server(AdminState(args.data_dir), args.port)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'PM × McKinsey Excel 上传后台: {url}\n仅本机可访问；保留此终端窗口。\n'
          f'本地数据: {args.data_dir}\n上传/导入不会自动发布线上。', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
