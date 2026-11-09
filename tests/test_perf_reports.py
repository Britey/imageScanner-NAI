"""Generated report charts must assign usable, matching bar and legend colors."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.perf_toolkit.report import write_reports


RENDER_CHART = r'''
const fs = require('node:fs'), vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8');
const elements = new Map(Object.entries({
  '#dataset': {value: 'backend'}, '#metric': {value: 'seconds'}, '#scale': {value: 'linear'},
  '#legend': {}, '#rows': {}, '#chart': {style: {}, setAttribute() {}},
}));
for (const element of elements.values()) element.addEventListener = () => {};
vm.runInNewContext(script, {document: {
  querySelector: selector => elements.get(selector) ?? null,
  querySelectorAll: () => ['#dataset', '#metric', '#scale'].map(key => elements.get(key)),
}}, {timeout: 1000});
console.log(JSON.stringify({legend: elements.get('#legend').innerHTML, chart: elements.get('#chart').innerHTML}));
'''


class PerformanceReportTests(unittest.TestCase):
    def test_three_or_more_collection_sizes_have_matching_defined_colors_and_unchanged_values(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Install Node.js 18+ to run generated report behavior tests')
        for size_count in (3, 6, 8):
            with self.subTest(collection_sizes=size_count), tempfile.TemporaryDirectory() as directory:
                sizes = [10 * (index + 1) for index in range(size_count)]
                summary = {
                    'environment': {'commit': 'abcdef012345', 'cpu': 'test CPU'},
                    'methodology': [], 'configuration': {'repeats': 1}, 'fixtures_removed': True,
                    'runs': [{
                        'fixture': {'image_count': count, 'sqlite_bytes': 1024**3},
                        'backend': {'stats': {
                            'successful_samples': 1, 'sample_count': 1,
                            'median_seconds': index + 1.25, 'min_seconds': index + 1,
                            'max_seconds': index + 1.5, 'max_peak_rss_mib': 12,
                        }},
                    } for index, count in enumerate(sizes)],
                }
                write_reports(summary, Path(directory))
                document = (Path(directory) / 'report.html').read_text()
                script = re.search(r'<script>(.*?)</script>', document, re.S).group(1)
                result = subprocess.run([node, '-e', RENDER_CHART], input=script, text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rendered = json.loads(result.stdout)
                properties = dict(re.findall(r'(--chart-\d+):([^;}]+)', document))
                legend = re.findall(r'background:var\((--chart-\d+)\).*?</i>(\d+) images', rendered['legend'])
                bars = re.findall(r'fill="var\((--chart-\d+)\)"><title>stats, (\d+) images: ([\d.]+) seconds', rendered['chart'])
                self.assertEqual([int(count) for _, count in legend], sizes)
                self.assertEqual([(color, int(count)) for color, count, _ in bars],
                                 [(color, int(count)) for color, count in legend])
                self.assertEqual([float(value) for _, _, value in bars],
                                 [index + 1.25 for index in range(size_count)])
                for color, _ in legend:
                    self.assertIn(color, properties, f'{color} has no CSS value, so bars and legend cannot use it')
                    self.assertRegex(properties[color], r'^#[0-9a-fA-F]{6}$')
                self.assertEqual(len({properties[color] for color, _ in legend}), min(size_count, 6))
