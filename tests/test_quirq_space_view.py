"""The quirq plugin's packaging: the committed Space view and the upload ZIP.

Standard library only, so it runs in every test run. The view
(plugins/quirq/ui/space-app.html) is generated from space_ui/ but committed,
because the GitHub marketplace installs the plugin folder from git. Whoever
changes space_ui/ or the bridge must rebuild it:

    python plugins/quirq/scripts/build_space_app.py
"""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "quirq"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = load('quirq_space_packager', ROOT / 'scripts/package_plugin.py')
builder = load('quirq_space_builder', ROOT / 'scripts/build_space_app.py')


class SpaceViewPackagingTests(unittest.TestCase):
    def test_committed_space_view_is_fresh(self):
        # The GitHub marketplace installs this folder from git, so the built
        # Space view is committed. Rebuild after space_ui/ or bridge changes:
        #   python plugins/quirq/scripts/build_space_app.py
        stamped = builder.built_digest()
        self.assertIsNotNone(stamped, 'ui/space-app.html is missing; run scripts/build_space_app.py')
        self.assertEqual(stamped, builder.source_digest(),
                         'ui/space-app.html is stale: run plugins/quirq/scripts/build_space_app.py and commit it')
        self.assertNotIn('AGENTS.md', {p.name for p in builder.source_files()})

    def test_archive_is_relocatable_and_excludes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'plugin.zip'
            packager.package(archive)
            with ZipFile(archive) as zipped:
                self.assertIsNone(zipped.testzip())
                names = zipped.namelist()
                self.assertIn('.codex-plugin/plugin.json', names)
                self.assertEqual(zipped.read('plugin.json'), zipped.read('.codex-plugin/plugin.json'))
                self.assertIn('.mcp.json', names)
                self.assertIn('ui/dashboard.html', names)
                self.assertIn('ui/space-bridge.js', names)
                self.assertIn('ui/space-app.html', names)
                self.assertIn('skills/quirq-onboarding/SKILL.md', names)
                self.assertNotIn('.agents/plugins/marketplace.json', names)
                self.assertFalse(any(n.startswith('plugins/') for n in names))
                self.assertFalse(any('.xo/' in n or '__pycache__' in n or n.endswith(('AGENTS.md', '.pyc', '.env')) for n in names))
                # Tests live in the repo's tests/, never in what users install.
                self.assertFalse(any(n.startswith('tests/') for n in names))
            packager.package(archive, marketplace=True)
            with ZipFile(archive) as zipped:
                marketplace = json.loads(zipped.read('.agents/plugins/marketplace.json'))
                source = marketplace['plugins'][0]['source']['path'][2:]
                self.assertIn(source + '/.codex-plugin/plugin.json', zipped.namelist())


if __name__ == '__main__':
    unittest.main()
