from __future__ import annotations

import concurrent.futures
import contextlib
import http.client
import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from imgdupe import web
from imgdupe.db import connect, init_db
from web_handler_fixture import WebFixture


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
        fourth = self.state.thumbs_dir / '4-cache.jpg'
        fourth.write_bytes(b'jpeg')
        with patch.object(Path, 'unlink', side_effect=OSError('busy')):
            self.state.thumbnail_created(fourth)
        self.assertEqual(self.state.thumbnail_cache_count, 3)
        self.assertEqual(len(list(self.state.thumbs_dir.glob('*.jpg'))), 3)

    def test_request_connections_are_lazy_separate_and_closed(self):
        self.conn.commit()
        with contextlib.closing(connect(self.state.db_path)) as disk:
            self.conn.backup(disk)
        self.state.concurrent_requests = True
        created = []
        original = web.connect
        def record_connection(path):
            connection = original(path)
            created.append(connection)
            return connection
        with patch.object(web, 'connect', record_connection):
            with self.state.request_context():
                pass
            self.assertEqual(created, [])
            for _ in range(2):
                with self.state.request_context():
                    connection = self.state.conn
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM images').fetchone()[0], 0)
                    self.assertIs(self.state.conn, connection)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
            self.assertEqual(len(created), 2)
            self.assertIsNot(created[0], created[1])
        self.assertIs(self.state.conn, self.conn)

    def test_status_poll_avoids_database_and_full_page(self):
        self.state.concurrent_requests = True
        with patch.object(web, 'connect', side_effect=AssertionError('status opened database')):
            status, _, body = self.response(self.request(path='/scan-status'))
        self.assertEqual(status, 200)
        self.assertIn('idle', body)
        self.state.scan_status = web.WebScanStatus(state='running')
        with patch.object(web, 'connect', side_effect=AssertionError('status opened database')):
            status, _, body = self.response(self.request(path='/scan-status'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'state': 'running'})

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
            for _ in range(server.max_request_workers):
                self.assertTrue(server._request_slots.acquire(blocking=False))
            self.assertFalse(server._request_slots.acquire(blocking=False))
            for _ in range(server.max_request_workers):
                server._request_slots.release()
        finally:
            server.server_close()


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

    @unittest.skipUnless(os.name == 'posix', 'requires symbolic links')
    def test_startup_ignores_broken_cache_links_and_still_prunes_old_thumbnails(self):
        thumbs = self.root / 'restored-cache'
        thumbs.mkdir()
        for index in range(4):
            path = thumbs / f'{index}.jpg'
            path.write_bytes(b'cached thumbnail')
            os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
        (thumbs / 'broken.jpg').symlink_to(thumbs / 'missing.jpg')
        restored = web.WebState(
            conn=self.conn, db_path=self.state.db_path, limit=10, min_score=55,
            thumbnail_size=32, work_dir=self.root / 'restored-work', thumbs_dir=thumbs,
            thumbnail_cache_limit=2, disable_advanced_options=False,
        )
        self.assertEqual(restored.thumbnail_cache_count, 2)
        self.assertEqual({path.name for path in restored._thumbnails}, {'2.jpg', '3.jpg'})
        self.assertFalse((thumbs / '0.jpg').exists())
        self.assertFalse((thumbs / '1.jpg').exists())
        latest = thumbs / '4.jpg'
        latest.write_bytes(b'new thumbnail')
        restored.thumbnail_created(latest)
        self.assertFalse((thumbs / '2.jpg').exists())
        self.assertEqual({path.name for path in restored._thumbnails}, {'3.jpg', '4.jpg'})

    def test_cache_permission_failures_preserve_served_bytes_and_recover(self):
        self.image()
        first = self.response(self.request(path='/thumb/1.jpg'))
        self.assertEqual(first[0], 200)
        with patch.object(web.os, 'utime', side_effect=PermissionError('read-only timestamp')):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        (self.state.thumbs_dir / 'external.jpg').write_bytes(b'created outside cache manager')
        existing = set(self.state.thumbs_dir.iterdir())
        with patch.object(Path, 'unlink', side_effect=PermissionError('read-only directory')):
            response = self.response(self.request('POST', '/clear-thumbnail-cache', {'lang': 'en'}))
        self.assertEqual(response[0], 303)
        self.assertIn('thumbnail_cache_cleared=0', response[1])
        self.assertEqual(set(self.state.thumbs_dir.iterdir()), existing)
        self.assertEqual(self.state.thumbnail_cache_count, 1)
        self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        self.assertEqual(self.state.clear_thumbnail_cache(), 2)
        self.assertEqual(self.state.thumbnail_cache_count, 0)

    def test_stale_thumbnail_removal_failure_does_not_prevent_new_version(self):
        self.image()
        stale = self.state.thumbs_dir / '1-stale.jpg'
        stale.write_bytes(b'stale')
        self.state.thumbnail_created(stale)
        unlink = Path.unlink
        def locked_stale(path, *args, **kwargs):
            if path == stale:
                raise PermissionError('old thumbnail is open')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', locked_stale):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        self.assertTrue(stale.exists())
        self.assertEqual(self.state.thumbnail_cache_count, 2)
        self.assertEqual(self.state.clear_thumbnail_cache(), 2)
        self.assertEqual(self.state.thumbnail_cache_count, 0)

    def test_failed_publication_with_locked_pending_file_is_retryable(self):
        self.image()
        replace, unlink = web.os.replace, Path.unlink
        def fail_publish(source, target):
            if str(target).endswith('.jpg'):
                raise OSError('publication unavailable')
            return replace(source, target)
        def locked_pending(path, *args, **kwargs):
            if path.suffix == '.pending':
                raise PermissionError('pending file is open')
            return unlink(path, *args, **kwargs)
        with patch.object(web.os, 'replace', fail_publish), patch.object(Path, 'unlink', locked_pending):
            self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 404)
        self.assertEqual(self.state.thumbnail_cache_count, 0)
        self.assertEqual(len(list(self.state.thumbs_dir.glob('*.pending'))), 1)
        self.assertEqual(self.response(self.request(path='/thumb/1.jpg'))[0], 200)
        self.assertEqual(list(self.state.thumbs_dir.glob('*.pending')), [])
        self.assertEqual(self.state.thumbnail_cache_count, 1)

    def test_startup_cache_pruning_tolerates_locked_older_entries(self):
        for index in range(3):
            path = self.state.thumbs_dir / f'{index}.jpg'
            path.write_bytes(b'cached')
            os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
        with patch.object(Path, 'unlink', side_effect=PermissionError('cache is read-only')):
            web._prune_thumbnail_cache(self.state.thumbs_dir, 1)
        self.assertEqual(len(list(self.state.thumbs_dir.glob('*.jpg'))), 3)
        web._prune_thumbnail_cache(self.state.thumbs_dir, 1)
        self.assertEqual([path.name for path in self.state.thumbs_dir.glob('*.jpg')], ['2.jpg'])
