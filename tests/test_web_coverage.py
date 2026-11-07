"""User workflows through rendered forms, HTTP parsing, and a real disk index."""
from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from imgdupe import web
from imgdupe.model_paths import build_config, load_model_paths, make_rule, save_model_paths
from test_web_support import WebTestCase, pattern_image, sd_parameters


class WebSearchTests(WebTestCase):
    def seed_prompts(self):
        for name, prompt, seed in (
            ('a.png', 'cat, smile, <script>alert1</script>', 1),
            ('b.png', 'dog, smile, cattle, catfish', 2),
            ('c.png', 'cat, smile, <script>alert1</script>', 3),
            ('d.png', 'cat, sleeping, cattle', 4),
        ):
            pattern_image(self.library / name, seed, prompt=sd_parameters(prompt))
        self.index()

    def names(self, response):
        self.assertEqual(response.status, 200, response.text)
        return [Path(card.find('p', class_='path').text()).name for card in response.page.cards]

    def test_rendered_search_form_filters_positive_and_negative_tags_and_models(self):
        self.seed_prompts()
        form = self.get('/?lang=en').page.form('/prompt-search')
        self.assertEqual(form.fields()['unique_prompts'], '1')
        self.assertEqual(self.names(self.submit(form, tags='cat', negative_tags='sleeping',
                                              sort_by='name_asc')), ['a.png'])
        self.assertEqual(self.names(self.submit(form, tags='cat', negative_tags='sleeping',
                                              unique_prompts='', sort_by='name_asc')), ['a.png', 'c.png'])
        self.assertEqual(self.names(self.submit(form, tags='cat | dog, smile',
                                              sort_by='name_asc')), ['a.png', 'b.png'])
        self.assertEqual(self.names(self.submit(form, tags='smile', model_type='nai4')), [])
        self.assertEqual(self.names(self.submit(form, tags='', negative_tags='cat')), ['b.png'])
        self.assertEqual(self.post('/prompt-search', {'tags': '', 'negative_tags': ''}).status, 400)

    def test_next_and_previous_forms_preserve_filters_and_return_distinct_pages(self):
        self.seed_prompts()
        first = self.post('/prompt-search', dict(tags='smile', negative_tags='sleeping',
                         sort_by='name_asc', limit='1', unique_prompts='1', model_type='sd', lang='en'))
        self.assertEqual(self.names(first), ['a.png'])
        next_form = next(form for form in first.page.forms if form.node.text() == 'Next')
        self.assertEqual(next_form.fields()['offset'], '1')
        for name, value in dict(tags='smile', negative_tags='sleeping', sort_by='name_asc',
                                limit='1', unique_prompts='1', model_type='sd').items():
            self.assertEqual(next_form.fields()[name], value)
        second = self.submit(next_form)
        self.assertEqual(self.names(second), ['b.png'])
        self.assertFalse(any(form.node.text() == 'Next' for form in second.page.forms))
        previous = next(form for form in second.page.forms if form.node.text() == 'Previous')
        self.assertEqual(self.names(self.submit(previous)), ['a.png'])

    def test_results_escape_markup_and_highlight_only_visible_matched_tags(self):
        self.seed_prompts()
        response = self.post('/prompt-search', dict(tags='cat', sort_by='name_asc', lang='en', unique_prompts='1'))
        self.assertEqual(self.names(response), ['a.png', 'd.png'])
        card = response.page.cards[0]
        self.assertEqual([node.text() for node in card.find_all('mark')], ['cat'])
        self.assertEqual(card.find_all('script'), [])
        self.assertIn('<script>alert1</script>', card.text())
        self.assertIn('&lt;script&gt;', response.text)
        model = self.post('/prompt-search', dict(tags='@model:sd', lang='en'))
        self.assertEqual(len(model.page.cards), 4)
        self.assertEqual(model.page.root.find_all('mark'), [])

    def test_autocomplete_uses_frequency_prefix_and_configured_model_visibility(self):
        self.seed_prompts()
        response = self.get('/suggest-tags?q=CA')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json, [{'tag': 'cat', 'count': 3},
                                         {'tag': 'cattle', 'count': 2},
                                         {'tag': 'catfish', 'count': 1}])
        self.assertEqual(self.get('/suggest-tags?q=').json, [])
        config = build_config((make_rule(self.library, 'Custom'),))
        self.index(model_paths=config)
        suggestions = self.get('/suggest-tags?q=%40model%3A').json
        self.assertIn({'tag': '@model:Custom', 'count': 4}, suggestions)
        self.assertNotIn('@model:sd', [item['tag'].casefold() for item in suggestions])
        self.assertEqual(self.get('/?lang=en').page.form('/prompt-search').options('model_type'), ['all', 'Custom'])

    def test_actual_image_upload_returns_the_matching_file_and_cleans_temporary_bytes(self):
        source = pattern_image(self.library / 'cat & friend.png', 11)
        pattern_image(self.library / 'unrelated.png', 12)
        self.index()
        for filename in ('query.png', 'query.unknown'):
            with self.subTest(filename=filename):
                observed = []
                real_query = web.query_image
                def query_uploaded_bytes(conn, path, **options):
                    observed.append((path.suffix, path.read_bytes()))
                    return real_query(conn, path, **options)
                with patch.object(web, 'query_image', query_uploaded_bytes):
                    response = self.upload(source.read_bytes(), {'min_score': '99'}, filename=filename)
                self.assertEqual(observed, [('.png' if filename.endswith('.png') else '.upload', source.read_bytes())])
                self.assertEqual(self.names(response), ['cat & friend.png'])
                self.assertIn('100.00', response.page.cards[0].text())
                self.assertEqual(response.page.cards[0].find('a').get('href'), source.as_uri())
                self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        self.assertEqual(self.names(self.upload(source.read_bytes(), {'min_score': '99', 'include_exact': ''})), [])
        bad = self.upload(b'not a decodable image', filename='broken.png')
        self.assertEqual(bad.status, 400)
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        self.assertEqual(self.upload(None).status, 400)

    def test_thumbnail_route_returns_source_pixels_reuses_cache_and_can_be_cleared(self):
        source = self.library / 'wide.png'
        Image.new('RGB', (80, 40), 'red').save(source)
        self.index()
        image_id = self.image_id(source)
        stale = self.state.thumbs_dir / f'{image_id}-old-version.jpg'
        stale.write_bytes(b'old thumbnail')
        self.state.thumbnail_created(stale)
        response = self.get(f'/thumb/{image_id}.jpg')
        self.assertFalse(stale.exists())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header('Content-Type'), 'image/jpeg')
        with Image.open(io.BytesIO(response.body)) as thumbnail:
            self.assertEqual(thumbnail.size, (32, 32))
            center = thumbnail.getpixel((16, 16))
            self.assertGreater(center[0], 230)
            self.assertLess(max(center[1:]), 20)
            self.assertTrue(all(channel > 230 for channel in thumbnail.getpixel((16, 0))))
        with patch.object(web, '_write_thumbnail', side_effect=AssertionError('cached image was regenerated')):
            self.assertEqual(self.get(f'/thumb/{image_id}.jpg').body, response.body)
        clear = self.get('/?lang=en').page.form('/clear-thumbnail-cache')
        cleared = self.submit(clear)
        self.assertEqual(cleared.status, 303)
        self.assertEqual(list(self.state.thumbs_dir.iterdir()), [])
        self.assertIn('Cleared 1', self.get(cleared.header('Location')).page.text)
        source.unlink()
        self.state.clear_thumbnail_cache()
        self.assertEqual(self.get(f'/thumb/{image_id}.jpg').status, 404)
        for path in ('/thumb/99999.jpg', '/thumb/bad', '/unknown'):
            self.assertEqual(self.get(path).status, 404)


class WebScanTests(WebTestCase):
    def finish(self):
        self.state.scan_thread.join(15)
        self.assertFalse(self.state.scan_thread.is_alive(), 'background scan did not finish')
        status = self.state.get_scan_status()
        self.assertEqual(status.state, 'completed', status.error)
        self.assertIsNotNone(status.started_at)
        self.assertIsNotNone(status.finished_at)
        return status.stats

    def classify(self):
        form = self.get('/?lang=en').page.form('/scan')
        response = self.submit(form, roots=str(self.library), workers='1')
        self.assertEqual(response.status, 200, response.text)
        self.assertFalse(self.model_paths_path.exists())
        self.assertIsNone(self.state.scan_thread)
        return response.page.form('/scan-configure')

    def test_classification_roundtrip_saves_rule_scans_and_supports_configured_rescan(self):
        source = pattern_image(self.library / 'cat.png', 9, prompt=sd_parameters('cat, smile'))
        form = self.classify()
        self.assertEqual(form.fields()['model_target_0'], str(self.library))
        response = self.submit(form, model_label_0='Custom')
        self.assertEqual(response.status, 303, response.text)
        self.assertEqual(self.finish().indexed, 1)
        self.assertEqual(load_model_paths(self.model_paths_path).resolve(source).label, 'Custom')
        row = self.conn.execute('SELECT width,height,length(sha256) FROM images').fetchone()
        self.assertEqual(tuple(row), (160, 120, 32))
        self.assertEqual(self.conn.execute('SELECT configured_model FROM image_prompts').fetchone()[0], 'Custom')
        result = self.post('/prompt-search', dict(tags='cat, @model:Custom', lang='en'))
        self.assertEqual(len(result.page.cards), 1)
        pattern_image(self.library / 'dog.png', 10, prompt=sd_parameters('dog'))
        form = self.get('/?lang=en').page.form('/scan-configured')
        self.assertEqual(self.submit(form).status, 303)
        self.assertEqual(self.finish().indexed, 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM images').fetchone()[0], 2)

    def test_stale_duplicate_incomplete_and_busy_classification_never_saves_rules(self):
        pattern_image(self.library / 'cat.png', 9)
        form = self.classify()
        values = form.fields()
        for changes in (
            {'model_target_0': str(self.root)},
            {'model_target_1': str(self.library)},
            {'model_target_0': ''},
        ):
            with self.subTest(changes=changes):
                response = self.submit(form, model_label_0='Custom', **changes)
                self.assertEqual(response.status, 400)
                self.assertFalse(self.model_paths_path.exists())
                self.assertIsNone(self.state.scan_thread)
        incomplete = {key: value for key, value in values.items() if key != 'model_target_0'}
        self.assertEqual(self.post('/scan-configure', incomplete).status, 400)
        self.state.finish_scan(web.WebScanStatus(state='running'))
        self.assertEqual(self.submit(form, model_label_0='Custom').status, 400)
        self.assertFalse(self.model_paths_path.exists())
        self.state.finish_scan(web.WebScanStatus())
        save_model_paths(self.model_paths_path, build_config((make_rule(self.library, 'Already saved'),)))
        original = self.model_paths_path.read_bytes()
        self.assertEqual(self.submit(form, model_label_0='Custom').status, 400)
        self.assertEqual(self.model_paths_path.read_bytes(), original)

    def test_running_scan_rejects_second_start_and_persists_completed_results(self):
        source = pattern_image(self.library / 'cat.png', 9, prompt=sd_parameters('cat'))
        save_model_paths(self.model_paths_path, build_config((make_rule(self.library, 'Custom'),)))
        entered, release = threading.Event(), threading.Event()
        original = web.scan_roots
        def held_scan(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release scan')
            return original(*args, **kwargs)
        with patch.object(web, 'scan_roots', held_scan):
            try:
                self.assertEqual(self.post('/scan', dict(roots=str(self.library), workers='1', prompt_index='1')).status, 303)
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.get('/scan-status').json, {'state': 'running'})
                second = self.post('/scan', dict(roots=str(self.library), workers='1'))
                self.assertEqual(second.status, 400)
                self.assertIn('already running', second.page.text)
            finally:
                release.set()
            self.assertEqual(self.finish().indexed, 1)
        self.assertEqual(self.image_id(source), 1)
        self.assertEqual(self.get('/scan-status').json, {'state': 'completed'})

    def test_background_failure_is_reported_and_next_scan_can_succeed(self):
        pattern_image(self.library / 'cat.png', 9)
        request = web.ScanRequest((self.library,), 1, False, True, False, False, False, 'en')
        with patch.object(web, 'connect', side_effect=OSError('database unavailable')):
            self.assertTrue(self.state.start_scan(request))
            self.state.scan_thread.join(5)
        status = self.state.get_scan_status()
        self.assertEqual(status.state, 'failed')
        self.assertIn('database unavailable', status.error)
        self.assertIn('database unavailable', self.get('/?lang=en').page.text)
        self.assertTrue(self.state.start_scan(request))
        self.assertEqual(self.finish().indexed, 1)

    def test_failures_page_reports_corrupt_images_and_folder_action_uses_indexed_path(self):
        broken = self.library / 'broken.png'
        broken.write_bytes(b'not PNG')
        good = pattern_image(self.library / 'good.png', 9)
        self.index()
        page = self.get('/failures?lang=en').page
        self.assertIn(str(broken), page.text)
        self.assertNotIn(str(good), page.text)
        with patch.object(web, '_show_path_in_folder') as show:
            self.assertEqual(self.post('/show-in-folder', {'id': self.image_id(good), 'lang': 'en'}).status, 200)
            show.assert_called_once_with(good)
            self.assertEqual(self.post('/show-in-folder', {'id': 'bad'}).status, 400)
            self.assertEqual(self.post('/show-in-folder', {'id': '9999'}).status, 404)
        self.assertEqual(self.get('/show-in-folder?id=1').status, 405)

class WebValidationTests(WebTestCase):
    def test_invalid_scan_roots_and_configured_rescan_never_start_or_modify_settings(self):
        for fields in ({}, {'roots': str(self.root / 'absent')}):
            response = self.post('/scan', fields)
            self.assertEqual(response.status, 400)
            self.assertIsNone(self.state.scan_thread)
            self.assertFalse(self.model_paths_path.exists())
        self.assertEqual(self.post('/scan-configured', {'lang': 'en'}).status, 400)
        self.model_paths_path.write_text('[broken')
        original = self.model_paths_path.read_bytes()
        for route in ('/scan', '/scan-configured'):
            response = self.post(route, {'roots': str(self.library), 'lang': 'en'})
            self.assertEqual(response.status, 400)
            self.assertIn('invalid', response.page.text)
            self.assertEqual(self.model_paths_path.read_bytes(), original)
            self.assertIsNone(self.state.scan_thread)

    def test_configuration_changed_to_invalid_during_validation_is_preserved(self):
        pattern_image(self.library / 'cat.png', 9)
        initial = self.post('/scan', {'roots': str(self.library), 'workers': '1'})
        form = initial.page.form('/scan-configure')
        original = web._unconfigured_scan_roots
        def externally_changed(roots):
            missing = original(roots)
            self.model_paths_path.write_text('[broken')
            return missing
        with patch.object(web, '_unconfigured_scan_roots', externally_changed):
            response = self.submit(form, model_label_0='Custom')
        self.assertEqual(response.status, 400)
        self.assertIn('invalid rules', response.page.text)
        self.assertEqual(self.model_paths_path.read_text(), '[broken')
        self.assertIsNone(self.state.scan_thread)

    def test_existing_classification_can_scan_without_rewriting_rules_and_busy_rescan_is_rejected(self):
        pattern_image(self.library / 'cat.png', 9)
        save_model_paths(self.model_paths_path, build_config((make_rule(self.library, 'Custom'),)))
        original = self.model_paths_path.read_bytes()
        response = self.post('/scan-configure', {'roots': str(self.library), 'workers': '1'})
        self.assertEqual(response.status, 303)
        self.state.scan_thread.join(10)
        self.assertFalse(self.state.scan_thread.is_alive())
        self.assertEqual(self.state.get_scan_status().state, 'completed')
        self.assertEqual(self.model_paths_path.read_bytes(), original)
        self.state.finish_scan(web.WebScanStatus(state='running'))
        response = self.post('/scan-configured', {'lang': 'en'})
        self.assertEqual(response.status, 400)
        self.assertIn('already running', response.page.text)
        self.assertEqual(self.model_paths_path.read_bytes(), original)
        self.state.finish_scan(web.WebScanStatus())

    def test_bad_upload_protocol_and_unknown_post_are_rejected(self):
        self.assertEqual(self.post('/search', {'lang': 'en'}).status, 400)
        self.assertEqual(self.post('/unknown', {}).status, 404)
        response = self.request('POST', '/search', body=b'body', headers=[('Content-Type', 'multipart/form-data')])
        self.assertEqual(response.status, 400)
        self.assertIn('missing multipart boundary', response.text)

    def test_upload_options_are_clamped_and_tryhard_changes_the_real_search_cutoff(self):
        source = pattern_image(self.library / 'cat.png', 9)
        self.index()
        original = web.query_image
        captured = []
        def actual_query(conn, path, **options):
            captured.append(options)
            return original(conn, path, **options)
        with patch.object(web, 'query_image', actual_query):
            result = self.upload(source.read_bytes(), {'tryhard': '1', 'min_score': '95', 'limit': '1000'})
            self.assertEqual(result.status, 200)
            self.assertEqual(len(result.page.cards), 1)
            result = self.upload(source.read_bytes(), {'quality': 'invalid', 'min_score': 'bad', 'limit': 'bad'})
            self.assertEqual(result.status, 200)
            self.assertEqual(len(result.page.cards), 1)
        self.assertEqual((captured[0]['tryhard'], captured[0]['min_score'], captured[0]['limit']), (True, 1, 500))
        self.assertEqual((captured[1]['tryhard'], captured[1]['min_score'], captured[1]['limit']), (False, 55, 10))

    def test_running_scan_panel_contains_status_and_shutdown_waits_for_real_work(self):
        pattern_image(self.library / 'cat.png', 9)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        actual = web.scan_roots
        def held_scan(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release background work')
            return actual(*args, **kwargs)
        def wait_for_scan():
            self.state.wait_for_scan()
            done.set()
        request = web.ScanRequest((self.library,), 1, False, True, False, False, False, 'en')
        with patch.object(web, 'scan_roots', held_scan):
            self.assertTrue(self.state.start_scan(request))
            self.assertTrue(entered.wait(5))
            waiter = threading.Thread(target=wait_for_scan)
            waiter.start()
            try:
                page = self.get('/?lang=en').page
                self.assertIn('running', page.root.find('p', class_='is-running').text().lower())
                self.assertFalse(done.wait(.05), 'shutdown returned while scan was blocked')
            finally:
                release.set()
                waiter.join(10)
            self.assertFalse(waiter.is_alive())
            self.assertTrue(done.is_set())
            self.assertEqual(self.state.get_scan_status().state, 'completed')
        self.assertIn('Cleared 0', self.get('/?lang=en&thumbnail_cache_cleared=invalid').page.text)

    def test_optional_and_unnamed_multipart_headers_do_not_change_uploaded_bytes(self):
        source = pattern_image(self.library / 'cat.png', 9)
        self.index()
        real_query = web.query_image
        observed = []
        def query_upload(conn, path, **options):
            observed.append((path.suffix, path.read_bytes()))
            return real_query(conn, path, **options)
        with patch.object(web, 'query_image', query_upload):
            for filename in (None, ''):
                self.assertEqual(self.upload(source.read_bytes(), filename=filename).status, 200)
            body, content_type = self.multipart({'csrf_token': self.state.csrf_token}, image=source.read_bytes())
            boundary = '----imgdupe-test-boundary'
            anonymous = f'--{boundary}\r\nContent-Disposition: attachment\r\n\r\nignore me\r\n'.encode()
            response = self.request('POST', '/search', body=anonymous + body,
                                    headers=[('Content-Type', content_type)])
            self.assertEqual(response.status, 200)
        self.assertEqual(observed, [('.upload', source.read_bytes()),
                                   ('.upload', source.read_bytes()), ('.png', source.read_bytes())])
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
