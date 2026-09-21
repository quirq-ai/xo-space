"""The marketplace must install a complete, relocatable Codex plugin bundle."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CodexPluginPackageTests(unittest.TestCase):
    def test_marketplace_resolves_complete_relocatable_bundle(self):
        marketplace = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        self.assertEqual(marketplace['name'], 'quirq-ai')
        entries = [p for p in marketplace['plugins'] if p['name'] == 'quirq']
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['policy'], {
            'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL',
        })
        self.assertEqual(entry['source']['source'], 'local')
        source = (ROOT / entry['source']['path']).resolve()
        self.assertTrue(source.is_relative_to(ROOT))
        with tempfile.TemporaryDirectory() as tmp:
            cached = (Path(tmp) / 'cache/quirq').resolve()
            shutil.copytree(source, cached)
            manifest = json.loads((cached / '.codex-plugin/plugin.json').read_text())
            self.assertEqual(manifest['name'], entry['name'])
            self.assertEqual(manifest['interface']['displayName'], 'XO Space')
            self.assertRegex(manifest['version'], r'^\d+\.\d+\.\d+(?:\+[a-zA-Z0-9.-]+)?$')
            paths = [manifest['skills']]
            interface = manifest['interface']
            paths += [interface[k] for k in ('composerIcon', 'logo', 'logoDark')]
            paths += interface['screenshots']
            for value in paths:
                resolved = (cached / value).resolve()
                self.assertTrue(resolved.is_relative_to(cached))
                self.assertTrue(resolved.exists(), value)
            skills = {p.parent.name for p in (cached / manifest['skills']).glob('*/SKILL.md')}
            self.assertEqual(skills, {'quirq', 'quirq-install', 'quirq-start', 'quirq-status'})
            for name in skills:
                skill = cached / manifest['skills'] / name / 'SKILL.md'
                self.assertIn(f'name: {name}\n', skill.read_text())
            for script in ('space.sh', 'discover.sh'):
                self.assertTrue((cached / 'scripts' / script).is_file())
            # Merely installing the package must not start a server in its cache.
            self.assertFalse((cached / 'hooks').exists())
            self.assertFalse((cached / '.mcp.json').exists())
            self.assertFalse((cached / 'server.py').exists())

    def test_claude_marketplace_still_resolves_its_original_bundle(self):
        marketplace = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
        entry = next(p for p in marketplace['plugins'] if p['name'] == 'quirq')
        self.assertEqual(entry['source'], './plugin')
        self.assertTrue((ROOT / entry['source'] / '.claude-plugin/plugin.json').is_file())

    def test_discovery_is_shared_without_drift(self):
        self.assertEqual((ROOT / 'plugin/scripts/discover.sh').read_bytes(),
                         (ROOT / 'plugins/quirq/scripts/discover.sh').read_bytes())


if __name__ == '__main__':
    unittest.main()
