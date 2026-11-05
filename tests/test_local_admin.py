"""Isolated local-console checks; never read or write the real online database."""
import http.client
import json
import os
import re
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import quote

import app
import local_admin as admin


class LocalAdminChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbook = (app.ROOT / 'EDGE_AI_Transformation_Tracker.xlsx').read_bytes()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.state = admin.AdminState(self.data_dir)
        self.server = admin.make_server(self.state, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        _, _, page = self.request('GET', '/')
        self.csrf = re.search(rb"const CSRF='([^']+)'", page)[1].decode()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def post_json(self, path, data):
        return self.request('POST', path, json.dumps(data),
                            {'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'})

    def preview(self, data=None, name='EDGE_AI_Transformation_Tracker.xlsx'):
        return self.request('POST', '/api/preview', data or self.workbook,
                            {'X-CSRF-Token': self.csrf, 'Content-Type': 'application/octet-stream',
                             'X-File-Name': quote(name)})

    def commit_first(self):
        status, _, body = self.preview()
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertIsNone(self.state.latest(), 'Preview must not save data')
        status, _, body = self.post_json('/api/import', {'previewId': preview['previewId']})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_empty_state_security_and_read_only_startup(self):
        status, _, body = self.request('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)['current'])
        self.assertEqual(self.request('GET', '/dashboard')[0], 404)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/status', headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/import', '{}', {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(self.request('POST', '/api/import', '{}',
                                      {'X-CSRF-Token': self.csrf, 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.post_json('/api/import', [1])[0], 400)
        self.assertEqual(self.request('POST', '/api/preview', b'a',
                                      {'X-CSRF-Token': self.csrf, 'Content-Length': str(admin.MAX_UPLOAD + 1)})[0], 413)
        self.assertEqual(self.request('GET', '/../../.env')[0], 404)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATABASE_URL', None)
            self.assertEqual(self.post_json('/api/online', {})[0], 503)

    def test_real_preview_commit_dashboard_and_restart(self):
        status, _, body = self.preview()
        preview = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(preview['snapshot']['records']), 10)
        self.assertEqual(preview['diff']['added'], 10)
        self.assertEqual(preview['diff']['removed'], 0)
        # Every parsed row keeps the dashboard's field and status validation.
        for row in preview['snapshot']['records']:
            self.assertIn(row['status'], app.STATUSES)
            self.assertTrue(row['workstream'] and row['intervention'])

        committed = self.commit_first()
        self.assertTrue(committed['changed'])
        self.assertEqual(committed['meta']['imported_rows'], 10)
        self.assertTrue(next((self.data_dir / 'uploads').glob('*.xlsx')).read_bytes() == self.workbook)

        # The browsable SQLite copy is readable by the real dashboard snapshot logic.
        browse_db = self.data_dir / 'tracker.sqlite3'
        self.assertTrue(browse_db.exists())
        self.assertEqual(len(app.snapshot(browse_db)['records']), 10)

        status, _, page = self.request('GET', '/dashboard')
        self.assertEqual(status, 200)
        self.assertIn(b'data-base-path="/dashboard"', page)
        self.assertEqual(self.request('GET', '/dashboard/api/data')[0], 200)
        status, asset_headers, asset = self.request('GET', '/dashboard/styles.css')
        self.assertEqual(status, 200)
        self.assertIn('text/css', asset_headers['Content-Type'])
        self.assertTrue(asset)

        # Re-uploading the identical workbook reports no data change but keeps history.
        _, _, repeated_body = self.preview()
        repeated = json.loads(repeated_body)
        self.assertTrue(repeated['identical'])
        self.assertFalse(json.loads(self.post_json('/api/import',
                                                   {'previewId': repeated['previewId']})[2])['changed'])
        restored = admin.AdminState(self.data_dir).status()
        self.assertEqual(len(restored['history']), 2)
        self.assertEqual(restored['current']['imported_rows'], 10)

    def test_bad_files_and_empty_data_keep_previous(self):
        self.commit_first()
        for data, name in [(b'not-a-workbook', 'bad.xlsx'),
                           (self.workbook, 'old.xls'),
                           (b'x' * 100, 'a/b/../../evil.xlsx')]:
            status, _, body = self.preview(data=data, name=name)
            self.assertEqual(status, 400, body)
        self.assertEqual(len(self.state.status()['history']), 1)

    def test_preview_token_expiry_and_conflict(self):
        _, _, body = self.preview()
        token = json.loads(body)['previewId']
        self.state.pending['expires'] = 0
        self.assertEqual(self.post_json('/api/import', {'previewId': token})[0], 409)
        self.assertEqual(self.post_json('/api/import', {'previewId': 'wrong-token'})[0], 409)

    def test_publish_requires_confirmation_and_rolls_back_on_failure(self):
        self.commit_first()
        local_version = self.state.status()['localVersion']
        with mock.patch.dict(os.environ, {'DATABASE_URL': 'mysql://u:p@mysql-host:3306/db'}):
            self.assertTrue(admin.mysql_store.configured())
            remote = self.state.latest()
            with mock.patch.object(admin.mysql_store, 'remote_snapshot', return_value=remote), \
                 mock.patch.object(admin.mysql_store, 'publish_snapshot', return_value=7) as publish:
                status, _, body = self.post_json('/api/online', {})
                self.assertEqual(status, 200)
                online = json.loads(body)
                self.assertEqual(online['localVersion'], local_version)
                status, _, body = self.post_json('/api/publish', {
                    'localVersion': online['localVersion'],
                    'remoteVersion': online['remoteVersion']})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['revision'], 7)
                publish.assert_called_once()
                receipt = self.state.status()['publication']
                self.assertEqual(receipt['localVersion'], local_version)

            # A stale remote version is rejected before any write happens.
            with mock.patch.object(admin.mysql_store, 'remote_snapshot', return_value=remote), \
                 mock.patch.object(admin.mysql_store, 'publish_snapshot') as publish:
                status, _, _ = self.post_json('/api/publish', {
                    'localVersion': local_version, 'remoteVersion': 'stale'})
                self.assertEqual(status, 409)
                publish.assert_not_called()

            # A failed publication surfaces as 503 and leaves the local receipt untouched.
            with mock.patch.object(admin.mysql_store, 'remote_snapshot', return_value=remote), \
                 mock.patch.object(admin.mysql_store, 'publish_snapshot', side_effect=RuntimeError('boom')):
                status, _, body = self.post_json('/api/online', {})
                online = json.loads(body)
                status, _, _ = self.post_json('/api/publish', {
                    'localVersion': online['localVersion'], 'remoteVersion': online['remoteVersion']})
                self.assertEqual(status, 503)

    def test_history_database_is_separate_from_browsable_copy(self):
        self.commit_first()
        with sqlite3.connect(self.data_dir / 'history.sqlite3') as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'imports', 'publications'})


if __name__ == '__main__':
    unittest.main()
