"""Offline behavioral regression tests; run with Python -B -m unittest discover."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import canvas_manager as M


class FakeCanvas:
    base = 'https://canvas.example'
    profile = 'test'

    def __init__(self):
        self.calls = []
        self.files = [dict(id=11, display_name='Lecture 2.pdf', size=4, updated_at='v1'),
                      dict(id=12, display_name='project.zip', size=4, updated_at='v1')]
        self.assignments = [dict(id=21, name='作业一', due_at='2026-10-01T16:00:00Z', description='<a href="/courses/1/files/11/download">附件</a>',
                                 submission=dict(workflow_state='submitted', submitted_at='2026-09-30T00:00:00Z'))]

    def probe(self):
        self.calls.append('probe')

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == '/api/v1/courses':
            return [dict(id=1, name='机器学习')]
        if path.endswith('/assignments'):
            return self.assignments
        if path.endswith('/files'):
            return self.files
        return self.files[0]

    def download(self, file_id, path, size, progress, updated_at=None):
        self.calls.append(('download', file_id))
        return M.atomic_new(path, [b'data'], size, progress)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = FakeCanvas()
        self.manager = M.Manager(self.api, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_is_read_only_and_detects_recursive_archive(self):
        path = self.root / '机器学习/slides/Week_02/deeper/Lecture 2.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'data')
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        report = self.manager.scan()
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertEqual(report['items'][0]['status'], 'exists')
        assignment = next(i for i in report['items'] if i['kind'] == 'assignment')
        self.assertEqual(assignment['submission'], '已提交')
        self.assertTrue(assignment['path'].endswith('2026-10-02_作业一'))
        self.assertNotIn('_contents', assignment)

    def test_only_selected_download_and_restart_index(self):
        self.manager.scan()
        result = self.manager.download_selected(['1:file:0:11'])
        self.assertEqual(result[0]['status'], 'downloaded')
        self.assertTrue((self.root / '机器学习/slides/Week_02/Lecture 2.pdf').exists())
        self.assertFalse((self.root / '机器学习/slides/project/project.zip').exists())
        restarted = M.Manager(self.api, self.root)
        report = restarted.scan()
        self.assertEqual(report['items'][0]['status'], 'exists')
        self.assertIn('SHA-256', report['items'][0]['reason'])
        self.assertNotIn('url', self.manager.index_path.read_text())

    def test_name_conflict_and_changed_version_preserve_original(self):
        path = self.root / '机器学习/slides/Week_02/Lecture 2.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'user content')
        self.manager.scan()
        result = self.manager.download_selected(['1:file:0:11'])
        self.assertEqual(path.read_bytes(), b'user content')
        self.assertEqual(result[0]['status'], 'downloaded')
        self.api.files[0]['updated_at'] = 'v2'
        restarted = M.Manager(self.api, self.root)
        restarted.scan()
        result2 = restarted.download_selected(['1:file:0:11'])
        self.assertEqual(result2[0]['status'], 'downloaded')
        self.assertNotEqual(result[0]['path'], result2[0]['path'])

    def test_same_size_local_corruption_is_not_treated_as_downloaded(self):
        self.manager.scan()
        result = self.manager.download_selected(['1:file:0:11'])
        (self.root / result[0]['path']).write_bytes(b'bad!')
        restarted = M.Manager(self.api, self.root)
        self.assertEqual(restarted.scan()['items'][0]['status'], 'pending')

    def test_failure_and_concurrent_target_leave_no_temp_or_overwrite(self):
        path = self.root / 'target.pdf'
        with self.assertRaises(M.ManagerError):
            M.atomic_new(path, [b'bad'], 4)
        self.assertFalse(path.exists())
        path.write_bytes(b'user')
        with self.assertRaises(FileExistsError):
            M.atomic_new(path, [b'data'], 4)
        self.assertEqual(path.read_bytes(), b'user')
        self.assertEqual([], list(self.root.glob('*.part')))

    def test_interrupted_stream_cleans_temporary_file(self):
        def chunks():
            yield b'one'
            raise OSError('disconnected')
        with self.assertRaises(OSError):
            M.atomic_new(self.root / 'out.pdf', chunks())
        self.assertEqual([], list(self.root.iterdir()))

    def test_assignment_snapshot_preserves_user_files_and_attachments_deduplicate(self):
        self.manager.scan()
        result = self.manager.download_selected(['1:assignment:21', '1:file:0:11', '1:attachment:21:11'])
        self.assertEqual([r['status'] for r in result], ['downloaded', 'downloaded', 'exists'])
        snapshot = self.root / result[0]['path'] / 'README.md'
        snapshot.write_text('my notes', encoding='utf-8')
        self.manager.download_selected(['1:assignment:21'])
        self.assertEqual(snapshot.read_text(encoding='utf-8'), 'my notes')
        self.assertTrue(snapshot.with_name('code').is_dir())

    def test_unknown_submissions_and_course_permission_failure_are_explicit(self):
        self.api.assignments[0]['submission'] = None
        report = self.manager.scan()
        self.assertEqual(next(i for i in report['items'] if i['kind'] == 'assignment')['submission'], '状态未知')
        original = self.api.get
        def restricted(path, params=None):
            if path.endswith('/assignments'):
                raise M.ManagerError('HTTP 403')
            return original(path, params)
        self.api.get = restricted
        report = self.manager.scan()
        self.assertTrue(report['errors'])
        self.assertTrue(report['items'])

    def test_probe_failure_stops_all_course_requests(self):
        def offline():
            raise M.ManagerError('000')
        self.api.probe = offline
        with self.assertRaises(M.ManagerError):
            self.manager.scan()
        self.assertFalse(self.api.calls)
        self.assertFalse(list(self.root.iterdir()))

    def test_path_escape_reserved_windows_names_and_media_limit(self):
        self.assertEqual(M.safe_name('CON.txt'), '_CON.txt')
        with self.assertRaises(M.ManagerError):
            M.Manager(self.api, self.root, {'1': '../escape'}).scan()
        self.api.files[0]['display_name'] = 'movie.mp4'
        self.api.files[1]['size'] = 151 * 1024 * 1024
        report = self.manager.scan()
        self.assertEqual([i['status'] for i in report['items'][:2]], ['skipped', 'skipped'])

    def test_lock_and_bad_selection(self):
        self.manager.scan()
        with self.assertRaises(M.ManagerError):
            self.manager.download_selected(['invalid'])
        lock = self.root / '.canvas-state/download.lock'
        lock.parent.mkdir()
        lock.write_text('other task')
        with self.assertRaises(M.ManagerError):
            self.manager.download_selected(['1:file:0:11'])
        self.assertEqual(lock.read_text(), 'other task')

    def test_identical_assignment_titles_get_separate_snapshots(self):
        self.api.assignments.append({**self.api.assignments[0], 'id':22})
        report = self.manager.scan()
        assignments = [i for i in report['items'] if i['kind'] == 'assignment']
        self.assertNotEqual(assignments[0]['path'], assignments[1]['path'])
        results = self.manager.download_selected([i['key'] for i in assignments])
        self.assertEqual([r['status'] for r in results], ['downloaded', 'downloaded'])
        restarted = M.Manager(self.api, self.root)
        paths = [i['path'] for i in restarted.scan()['items'] if i['kind']=='assignment']
        self.assertEqual(paths, [i['path'] for i in assignments])

    def test_remote_same_size_update_requires_new_scan(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'canvas_token':'test-only', 'base_url':self.api.base}))
        client = M.CanvasClient(config)
        with patch.object(client, 'get', return_value={'size':4, 'updated_at':'v2'}):
            with self.assertRaises(M.ManagerError):
                client.download(11, self.root/'out.pdf', 4, lambda n:None, 'v1')
        self.assertFalse((self.root/'out.pdf').exists())

    def test_duplicate_remote_names_do_not_guess_historical_identity(self):
        self.api.files[1]['display_name'] = self.api.files[0]['display_name']
        path = self.root / '机器学习/slides/Lecture 2.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'data')
        report = self.manager.scan()
        self.assertEqual([i['status'] for i in report['items'][:2]], ['pending', 'pending'])

    def test_api_pagination_does_not_forward_token_to_other_origin(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'canvas_token': 'test-only', 'base_url': self.api.base}))
        client = M.CanvasClient(config)
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = [dict(id=1)]
        response.links = {'next': {'url': 'https://evil.example/steal'}}
        with patch.object(M.requests, 'get', return_value=response) as get:
            with self.assertRaises(M.ManagerError):
                client.get('/api/v1/courses')
            self.assertEqual(get.call_count, 1)

    def test_local_http_scan_download_csrf_and_unsupported_submission(self):
        app = M.Application(self.manager)
        server = M.make_server(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        import requests
        url = f'http://127.0.0.1:{server.server_port}'
        headers = {'X-Canvas-CSRF': app.secret}
        try:
            self.assertEqual(requests.get(url, timeout=5).status_code, 200)
            self.assertEqual(requests.post(url+'/api/scan', json={}, timeout=5).status_code, 403)
            self.assertEqual(requests.post(url+'/api/submit', json={}, headers=headers, timeout=5).status_code, 404)
            self.assertEqual(requests.post(url+'/api/scan', json={}, headers={**headers, 'Origin':'https://evil.example'}, timeout=5).status_code, 403)
            self.assertEqual(requests.post(url+'/api/scan', json={}, headers=headers, timeout=5).status_code, 202)
            self.wait_task(app)
            payload = requests.get(url+'/api/state', timeout=5).json()
            self.assertTrue(payload['inventory']['items'])
            self.assertEqual(requests.post(url+'/api/download', json={'keys':['1:file:0:11']}, headers=headers, timeout=5).status_code, 202)
            self.wait_task(app)
            self.assertEqual(app.state['results'][0]['status'], 'downloaded')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def wait_task(self, app):
        deadline = time.monotonic() + 5
        while app.state['busy'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(app.state['busy'])


if __name__ == '__main__':
    unittest.main()
