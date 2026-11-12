"""Run: python -m unittest discover -s tests -v (uses temporary databases only)."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import app

SOURCE = app.ROOT / 'EDGE_AI_Transformation_Tracker.xlsx'


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'tracker.sqlite3'
        app.initialize(self.db, SOURCE)
        self.data = app.snapshot(self.db)

    def edit(self, index=0, **kwargs):
        row = self.data['records'][index]
        return {'id': row['id'], **{k: row[k] for k in app.FIELDS}, **kwargs}

    def save(self, changes, revision=1):
        return app.save_changes(self.db, {'revision': revision, 'changes': changes})

    def test_seed_matches_excel_excludes_blanks(self):
        rows = self.data['records']
        self.assertEqual(len(rows), 10)
        self.assertEqual([r['source_row'] for r in rows], list(range(2, 12)))
        self.assertEqual(sum(r['status'] == 'In Progress' for r in rows), 6)
        self.assertEqual(sum(r['status'] == 'Not Started' for r in rows), 3)
        self.assertEqual(sum(r['status'] == 'Pending Confirmation' for r in rows), 1)
        self.assertEqual(sum(bool(r['target_date']) for r in rows), 2)
        self.assertEqual(self.data['meta']['source_hash'], hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        self.assertEqual(rows[8]['workstream'], 'GR / billing and collection')
        self.assertEqual(rows[6]['workstream'], 'Tatical plan')

    def test_update_and_new_row_persist_together(self):
        row = self.edit(owner='New Owner', status='Delivered', progress='已获业务验收\n下一步：扩展')
        new = {**self.edit(), 'id': 'new-test', 'intervention': '新增交付事项', 'target_date': '2026-12-31'}
        result = self.save([row, new])
        self.assertEqual(result['saved_count'], 2)
        self.assertEqual(result['meta']['revision'], 2)
        self.assertEqual(len(result['records']), 11)
        self.assertEqual(result['records'][0]['owner'], 'New Owner')
        self.assertIsNone(result['records'][-1]['source_row'])
        self.assertFalse(result['records'][-1]['id'].startswith('new-'))
        self.assertEqual(app.snapshot(self.db)['records'], result['records'])
        with app.connect(self.db) as conn:
            audit = json.loads(conn.execute('SELECT changes_json FROM saves WHERE revision=2').fetchone()[0])
        self.assertEqual(audit[0]['before']['owner'], 'Tracy Ta')
        self.assertEqual(audit[0]['after']['owner'], 'New Owner')
        self.assertIsNone(audit[1]['before'])

    def test_restart_does_not_reimport_or_overwrite_edits(self):
        self.save([self.edit(progress='本地更新，重启必须保留')])
        app.initialize(self.db, Path('/missing/source-is-not-needed-after-seeding.xlsx'))
        self.assertEqual(app.snapshot(self.db)['records'][0]['progress'], '本地更新，重启必须保留')
        self.assertEqual(app.snapshot(self.db)['meta']['revision'], 2)

    def test_failed_batch_rolls_back_all_changes(self):
        good = self.edit(owner='Must Roll Back')
        new = {**self.edit(), 'id': 'new-rollback'}
        missing = {**self.edit(), 'id': 'missing-id'}
        with self.assertRaises(app.ValidationError):
            self.save([good, new, missing])
        self.assertEqual(app.snapshot(self.db), self.data)
        with app.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM saves').fetchone()[0], 1)

    def test_invalid_fields_and_dates_reject_entire_batch(self):
        bad_values = [{'status': 'Completed'}, {'target_date': '2026-02-30'},
                      {'target_date': '20261130'}, {'target_date': '2026-1-1'},
                      {'workstream': '   '}, {'intervention': ''}, {'owner': 123},
                      {'progress': 'x' * 20001}, {'blocker': '\x00'}, {'unknown': 'x'}]
        for bad in bad_values:
            with self.subTest(bad=list(bad)):
                with self.assertRaises(app.ValidationError):
                    self.save([self.edit(owner='Do Not Save'), self.edit(1, **bad)])
                self.assertEqual(app.snapshot(self.db), self.data)

    def test_duplicate_id_rejected(self):
        with self.assertRaises(app.ValidationError):
            self.save([self.edit(), self.edit(owner='Duplicate')])
        self.assertEqual(app.snapshot(self.db), self.data)

    def test_no_op_does_not_invent_version_or_history(self):
        result = self.save([self.edit()])
        self.assertEqual(result['meta']['revision'], 1)
        self.assertEqual(result['saved_count'], 0)
        self.assertEqual(result['records'], self.data['records'])

    def test_stale_version_never_overwrites_other_window(self):
        self.save([self.edit(owner='Window One')])
        with self.assertRaises(app.ConflictError):
            self.save([self.edit(owner='Window Two')])
        self.assertEqual(app.snapshot(self.db)['records'][0]['owner'], 'Window One')

    def test_concurrent_writers_only_one_can_save(self):
        def attempt(owner):
            try:
                self.save([self.edit(owner=owner)])
                return 'ok'
            except app.ConflictError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(attempt, ['First', 'Second']), ['ok', 'conflict'])
        self.assertEqual(app.snapshot(self.db)['meta']['revision'], 2)

    def test_sql_and_html_are_stored_as_text(self):
        literal = "<img src=x onerror=alert(1)>'); DROP TABLE interventions; --"
        self.save([self.edit(progress=literal)])
        data = app.snapshot(self.db)
        self.assertEqual(data['records'][0]['progress'], literal)
        self.assertEqual(len(data['records']), 10)

    def test_source_workbook_remains_unchanged(self):
        before = SOURCE.read_bytes()
        self.save([self.edit(progress='Database-only edit')])
        self.assertEqual(before, SOURCE.read_bytes())

    def test_invalid_seed_is_not_half_imported(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Tracker'
        sheet.append(app.HEADERS)
        sheet.append(['Enabler', 'Valid', '', '', 'Not Started'])
        sheet.append(['Enabler', 'Invalid', '', '', 'Bad Status'])
        file = Path(self.temp.name) / 'invalid.xlsx'
        workbook.save(file)
        other = Path(self.temp.name) / 'invalid.sqlite3'
        with self.assertRaises(app.ValidationError):
            app.initialize(other, file)
        with app.connect(other) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM interventions').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metadata').fetchone()[0], 0)

    def test_http_endpoints_same_origin_and_validation(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        server.db = self.db
        server.base_path = '/ptsd-00350'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port

        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', port)
            try:
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        self.assertEqual(request('GET', '/health')[0], 200)
        self.assertEqual(len(json.loads(request('GET', '/ptsd-00350/api/data')[1])['records']), 10)
        self.assertEqual(request('GET', '/data/tracker.sqlite3')[0], 404)
        self.assertEqual(request('GET', '/../app.py')[0], 404)
        html = request('GET', '/ptsd-00350')[1].decode()
        self.assertIn('data-base-path="/ptsd-00350"', html)
        self.assertIn('src="/ptsd-00350/app.js"', html)
        self.assertNotIn('__BASE_PATH__', html)
        payload = json.dumps({'revision': 1, 'changes': [self.edit(owner='HTTP Owner')]})
        self.assertEqual(request('POST', '/api/save', payload, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(request('POST', '/api/save', payload, {'Content-Type': 'application/json', 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(request('GET', '/api/data', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        nav = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
        self.assertEqual(request('GET', '/ptsd-00350', headers=nav)[0], 200)
        self.assertEqual(request('GET', '/ptsd-00350/api/data', headers=nav)[0], 403)
        self.assertEqual(request('POST', '/api/save', '{', {'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(request('POST', '/api/save', payload, {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{port}'})[0], 200)
        self.assertEqual(request('POST', '/api/save', payload, {'Content-Type': 'application/json'})[0], 409)
        self.assertEqual(app.snapshot(self.db)['records'][0]['owner'], 'HTTP Owner')


if __name__ == '__main__':
    unittest.main()
