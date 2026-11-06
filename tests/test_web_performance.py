from __future__ import annotations

import concurrent.futures
import http.client
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from imgdupe import cli, web
from imgdupe.db import connect, init_db
from test_web_coverage import WebFixture


class WebPerformanceTests(WebFixture):
    def test_atomic_thumbnail_failure_can_retry_and_preserves_existing(self):
        source = self.root / 'source.png'
        Image.new('RGB', (64, 64), 'red').save(source)
        target = self.state.thumbs_dir / 'atomic.jpg'

        def partial_save(image, output, *args, **kwargs):
            output.write(b'partial JPEG')
            raise OSError('disk full')

        with patch.object(Image.Image, 'save', partial_save):
            self.assertFalse(web._write_thumbnail(source, target, 32))
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])
        self.assertTrue(web._write_thumbnail(source, target, 32))
        previous = target.read_bytes()
        with patch.object(Image.Image, 'save', partial_save):
            self.assertFalse(web._write_thumbnail(source, target, 32))
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cache_miss_uses_index_and_warm_hit_is_most_recent(self):
        self.image()
        with patch.object(Path, 'glob', side_effect=AssertionError('per-request directory walk')):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        cached = next(iter(self.state._thumbnails))
        second = self.state.thumbs_dir / '2-cache.jpg'
        second.write_bytes(b'jpeg')
        self.state.thumbnail_created(second)
        self.response(self.request(path='/thumb/1.jpg'))
        third = self.state.thumbs_dir / '3-cache.jpg'
        third.write_bytes(b'jpeg')
        self.state.thumbnail_created(third)
        self.assertTrue(cached.exists())
        self.assertFalse(second.exists())
        with patch.object(Path, 'unlink', side_effect=OSError('busy')):
            self.state.thumbnail_created(second)
        self.assertEqual(self.state.thumbnail_cache_count, 3)

    def test_request_connections_are_lazy_separate_and_closed(self):
        self.state.concurrent_requests = True
        with patch.object(web, 'connect') as factory:
            with self.state.request_context():
                pass
            factory.assert_not_called()
            for _ in range(2):
                connection = Mock()
                factory.return_value = connection
                with self.state.request_context():
                    self.assertIs(self.state.conn, connection)
                    self.assertIs(self.state.conn, connection)
                connection.close.assert_called_once()
            self.assertEqual(factory.call_count, 2)
        self.assertIs(self.state.conn, self.conn)

    def test_status_poll_avoids_database_and_full_page(self):
        self.state.concurrent_requests = True
        with patch.object(web, 'connect', side_effect=AssertionError('status opened database')):
            status, _, body = self.response(self.request(path='/scan-status'))
        self.assertEqual(status, 200)
        self.assertIn('idle', body)
        self.state.scan_status = web.WebScanStatus(state='running')
        panel = web._scan_panel(self.state, 'en')
        self.assertIn("fetch('/scan-status')", panel)
        self.assertNotIn('setTimeout(() => window.location.reload()', panel)

    def test_show_folder_platform_and_missing_launcher(self):
        path = self.root / 'picture.png'
        for platform, command in [('linux', 'xdg-open'), ('darwin', 'open')]:
            with patch.object(web.sys, 'platform', platform), patch.object(web.subprocess, 'Popen') as launch:
                web._show_path_in_folder(path)
            self.assertEqual(launch.call_args.args[0], [command, str(path.parent)])
        self.image()
        with patch.object(web, '_show_path_in_folder', side_effect=FileNotFoundError('launcher unavailable')):
            handler = self.request(path='/show-in-folder', method='POST', fields={'id': '1'})
            status, _, body = self.response(handler)
        self.assertEqual(status, 500)
        self.assertIn('launcher unavailable', body)

    def test_slow_search_does_not_block_another_request(self):
        self.conn.commit()
        disk = connect(self.state.db_path)
        self.conn.backup(disk)
        disk.close()
        self.state.concurrent_requests = True
        server = web.HTTPServer(('127.0.0.1', 0), web._make_handler(self.state))
        self.state.server_port = server.server_port
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        entered, release = threading.Event(), threading.Event()

        def slow_suggestions(state, query):
            # Read from a connection owned by this handler thread.
            state.conn.execute('SELECT COUNT(*) FROM images').fetchone()
            entered.set()
            self.assertTrue(release.wait(5))
            return []

        def get(path):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                client.request('GET', path)
                response = client.getresponse()
                return response.status, response.read()
            finally:
                client.close()

        try:
            with patch.object(web, '_tag_suggestions', slow_suggestions), concurrent.futures.ThreadPoolExecutor() as executor:
                slow = executor.submit(get, '/suggest-tags?q=x')
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(get('/scan-status')[0], 200)
                finally:
                    release.set()
                self.assertEqual(slow.result()[0], 200)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join()

    def test_worker_slots_released_when_thread_cannot_start(self):
        server = web.HTTPServer(('127.0.0.1', 0), web._make_handler(self.state))
        try:
            with patch.object(web.ThreadingMixIn, 'process_request', side_effect=RuntimeError('thread unavailable')):
                with self.assertRaises(RuntimeError):
                    server.process_request(Mock(), ('127.0.0.1', 1))
            self.assertEqual(server._request_slots._value, server.max_request_workers)
        finally:
            server.server_close()

    def test_cli_rejects_nonpositive_limits_before_database_open(self):
        for command in ['query', 'tags']:
            for limit in ['0', '-1']:
                args = [command, '--db', str(self.root / 'never.db'), '--limit', limit]
                args += ['example.png'] if command == 'query' else ['cat']
                with patch.object(cli, 'connect') as connection, self.assertRaises(SystemExit) as error:
                    cli.main(args)
                self.assertEqual(error.exception.code, 2)
                connection.assert_not_called()

    def test_existing_cache_restores_lru_and_multiple_versions(self):
        first = self.state.thumbs_dir / '1-first.jpg'
        second = self.state.thumbs_dir / '1-second.jpg'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        restored = web.WebState(
            conn=self.conn, db_path=self.state.db_path, limit=10, min_score=55,
            thumbnail_size=32, work_dir=self.root / 'work', thumbs_dir=self.state.thumbs_dir,
            thumbnail_cache_limit=2, disable_advanced_options=False,
        )
        self.assertEqual(restored.thumbnail_cache_count, 2)
        restored.thumbnail_removed(first)
        self.assertEqual(restored._thumbnail_versions['1'], {second})

    def test_clear_during_generation_does_not_remove_pending_publication(self):
        self.image()
        writer = web._write_thumbnail

        def clearing_writer(source, target, size):
            result = writer(source, target, size)
            self.state.clear_thumbnail_cache()
            return result

        with patch.object(web, '_write_thumbnail', clearing_writer):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        self.assertEqual(self.state.thumbnail_cache_count, 1)
        self.assertEqual(len(list(self.state.thumbs_dir.iterdir())), 1)

    def test_thumbnail_publication_failure_returns_error_and_cleans_pending(self):
        self.image()
        replace = web.os.replace

        def reject_publication(source, target):
            if str(target).endswith('.jpg'):
                raise OSError('publish failed')
            return replace(source, target)

        with patch.object(web.os, 'replace', reject_publication):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 404)
        self.assertEqual(list(self.state.thumbs_dir.iterdir()), [])
        with patch.object(Path, 'unlink', side_effect=OSError('read-only directory')):
            web._discard_thumbnail(self.state.thumbs_dir / 'absent.tmp')
