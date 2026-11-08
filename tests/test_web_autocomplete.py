"""Exercise the rendered production JS, rather than asserting source strings."""
import json
from pathlib import Path
import shutil
import subprocess

from test_web_support import WebTestCase


class AutocompleteTests(WebTestCase):
    def test_keyboard_click_debounce_and_out_of_order_fetch_behavior(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Install Node.js 18+ to run the rendered JavaScript behavior tests')
        response = self.get('/?lang=en')
        self.assertEqual(response.status, 200)
        page = response.page
        payload = json.dumps({
            'script': page.scripts()[-1],
            'elements': [{'tag': node.tag, 'attrs': node.attrs} for node in page.root.iter()],
        })
        result = subprocess.run(
            [node, str(Path(__file__).with_name('web_autocomplete.cjs'))],
            input=payload, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PASS '), 10, result.stdout)
