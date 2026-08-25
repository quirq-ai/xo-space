from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.cowork_agent import runtime_config
from services.cowork_agent.visualizer import watcher


def _manifest(name: str, home: Path) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        binary=f"{name}-bin",
        home_dir=home,
        raw={
            "install_url": "https://example.test/install",
            "runtime_setup": {
                "session_globs": ["sessions/**/*.jsonl"],
                "secrets": [
                    {
                        "key": "TEST_API_KEY",
                        "label": "Test API key",
                        "description": "Only a test.",
                    }
                ],
            }
        },
    )


class RuntimeConfigTests(unittest.TestCase):
    def test_root_settings_are_private_validated_and_restart_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "current-state"
            state_root.mkdir()
            projects_root = Path(tmp) / "projects"
            next_state_root = Path(tmp) / "next-state"
            env = {
                "QUIRQ_STATE_ROOT": str(state_root),
                "QUIRQ_HOST_PROJECTS_ROOT": str(projects_root),
                "QUIRQ_HOST_STATE_ROOT": str(state_root),
            }
            with patch.dict(os.environ, env, clear=False):
                saved = runtime_config.save_root_settings(
                    {
                        "xo_projects_root": str(projects_root),
                        "quirq_state_root": str(next_state_root),
                    }
                )
                status = runtime_config.root_settings()

            self.assertEqual(saved["quirq_state_root"], str(next_state_root))
            self.assertTrue(status["change_required"])
            self.assertEqual(
                status["configured"]["xo_projects_root"],
                str(projects_root),
            )
            root_file = state_root / "roots.env"
            self.assertEqual(stat.S_IMODE(root_file.stat().st_mode), 0o600)
            self.assertIn(
                f"QUIRQ_STATE_ROOT={next_state_root}",
                root_file.read_text(encoding="utf-8"),
            )

    def test_roots_must_be_absolute_separate_and_non_nested(self) -> None:
        invalid = (
            {"xo_projects_root": "relative", "quirq_state_root": "/tmp/state"},
            {"xo_projects_root": "/", "quirq_state_root": "/tmp/state"},
            # equal roots
            {"xo_projects_root": "/tmp/xo", "quirq_state_root": "/tmp/xo"},
            # nested deeper than one level
            {"xo_projects_root": "/tmp/xo", "quirq_state_root": "/tmp/xo/deep/.quirq"},
            # nested one level but not hidden
            {"xo_projects_root": "/tmp/xo", "quirq_state_root": "/tmp/xo/quirq"},
            # projects root inside the state root
            {
                "xo_projects_root": "/tmp/xo/.quirq/projects",
                "quirq_state_root": "/tmp/xo/.quirq",
            },
        )
        for payload in invalid:
            with self.assertRaises(ValueError):
                runtime_config.validate_root_settings(payload)

    def test_state_root_may_be_hidden_directly_inside_projects_root(self) -> None:
        # The default layout install.sh itself creates: ./ and ./.quirq. The
        # API validator must accept what the installer's validator accepts,
        # or Setup rejects the configuration the server is running with.
        clean = runtime_config.validate_root_settings(
            {"xo_projects_root": "/tmp/xo", "quirq_state_root": "/tmp/xo/.quirq"}
        )
        self.assertEqual(clean["xo_projects_root"], "/tmp/xo")
        self.assertEqual(clean["quirq_state_root"], "/tmp/xo/.quirq")

    def test_save_is_private_validated_and_restart_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "runtime.env"
            first = _manifest("first", Path(tmp) / "first")
            second = _manifest("second", Path(tmp) / "second")
            env = {
                "QUIRQ_WATCHER_ENABLED": "true",
                "QUIRQ_WATCHER_INTERVAL_SECONDS": "1",
                "QUIRQ_WATCHER_SOURCE_MODE": "all",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(runtime_config, "runtime_config_file", return_value=config_file),
                patch.object(runtime_config, "all_agents", return_value=[first, second]),
                patch.object(runtime_config, "get_active_agent", return_value=first),
            ):
                saved = runtime_config.save_settings(
                    {
                        "agent_name": "second",
                        "watcher_enabled": False,
                        "watcher_interval_seconds": 2.5,
                        "watcher_source_mode": "active",
                    }
                )
                self.assertEqual(saved["agent_name"], "second")
                self.assertTrue(runtime_config.restart_required())

            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            text = config_file.read_text(encoding="utf-8")
            self.assertIn("AGENT_NAME=second", text)
            self.assertIn("QUIRQ_WATCHER_ENABLED=false", text)
            self.assertIn("QUIRQ_WATCHER_INTERVAL_SECONDS=2.5", text)
            self.assertIn("QUIRQ_WATCHER_SOURCE_MODE=active", text)

    def test_invalid_agent_interval_and_source_mode_are_rejected(self) -> None:
        manifest = _manifest("only", Path("/tmp/only"))
        with patch.object(runtime_config, "all_agents", return_value=[manifest]):
            for payload in (
                {
                    "agent_name": "missing",
                    "watcher_enabled": True,
                    "watcher_interval_seconds": 1,
                    "watcher_source_mode": "all",
                },
                {
                    "agent_name": "only",
                    "watcher_enabled": True,
                    "watcher_interval_seconds": 0.1,
                    "watcher_source_mode": "all",
                },
                {
                    "agent_name": "only",
                    "watcher_enabled": True,
                    "watcher_interval_seconds": 1,
                    "watcher_source_mode": "everything",
                },
            ):
                with self.assertRaises(ValueError):
                    runtime_config.validate_settings(payload)

    def test_secret_store_change_requires_restart_without_exposing_values(self) -> None:
        initial = [{"key": "TEST_API_KEY", "value": "before"}]
        changed = [{"key": "TEST_API_KEY", "value": "after"}]
        with patch.object(runtime_config, "load_env_entries", return_value=initial):
            initial_fingerprint = runtime_config._secrets_fingerprint()
        with (
            patch.object(runtime_config, "load_env_entries", return_value=changed),
            patch.object(
                runtime_config,
                "_STARTUP_SECRETS_FINGERPRINT",
                initial_fingerprint,
            ),
            patch.object(
                runtime_config,
                "_runtime_settings_restart_required",
                return_value=False,
            ),
        ):
            self.assertTrue(runtime_config.secrets_restart_required())
            with patch.object(
                runtime_config,
                "roots_restart_required",
                return_value=False,
            ):
                self.assertEqual(runtime_config.restart_reasons(), ["secrets"])

    def test_source_diagnostics_count_sessions_without_reading_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "runtime"
            session_dir = home / "sessions" / "project"
            session_dir.mkdir(parents=True)
            (session_dir / "one.jsonl").write_text("{}\n", encoding="utf-8")
            manifest = _manifest("only", home)
            with (
                patch.dict(
                    os.environ,
                    {"QUIRQ_WATCHER_SOURCE_MODE": "all"},
                    clear=False,
                ),
                patch.object(runtime_config, "all_agents", return_value=[manifest]),
                patch.object(runtime_config, "get_active_agent", return_value=manifest),
                patch.object(
                    runtime_config,
                    "load_env_entries",
                    return_value=[{"key": "TEST_API_KEY", "value": "redacted"}],
                ),
                patch.object(runtime_config.shutil, "which", return_value="/bin/tool"),
            ):
                rows = runtime_config.runtime_sources()

            self.assertEqual(rows[0]["session_files"], 1)
            self.assertTrue(rows[0]["watched"])
            self.assertTrue(rows[0]["secrets"][0]["configured"])
            self.assertNotIn("redacted", repr(rows))
            # the manifest's own install docs ride along for the Setup card
            self.assertEqual(rows[0]["install_url"], "https://example.test/install")

    def test_source_without_install_url_reports_none(self) -> None:
        """No manifest field → no link; the card must not invent one."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _manifest("bare", Path(tmp) / "runtime")
            del manifest.raw["install_url"]
            with (
                patch.object(runtime_config, "all_agents", return_value=[manifest]),
                patch.object(runtime_config, "get_active_agent", return_value=manifest),
                patch.object(runtime_config, "load_env_entries", return_value=[]),
                patch.object(runtime_config.shutil, "which", return_value=None),
            ):
                rows = runtime_config.runtime_sources()

            self.assertIsNone(rows[0]["install_url"])
            self.assertFalse(rows[0]["binary_available"])

    def test_applied_roots_come_from_the_shared_project_layout_helper(self) -> None:
        """Setup must report the root the rest of the app resolves, not a
        second copy of the env lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            projects_root = Path(tmp) / "projects"
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            env = {
                "XO_PROJECTS_ROOT": str(projects_root),
                "QUIRQ_STATE_ROOT": str(state_root),
            }
            for stale in ("QUIRQ_HOST_PROJECTS_ROOT", "QUIRQ_HOST_STATE_ROOT"):
                os.environ.pop(stale, None)
            with patch.dict(os.environ, env, clear=False):
                from services.cowork_agent.project_layout import xo_projects_root

                applied = runtime_config.applied_roots()
                self.assertEqual(
                    applied["xo_projects_root"],
                    str(xo_projects_root()),
                )
                # Nothing saved yet: configured mirrors applied, no restart.
                status = runtime_config.root_settings()
                self.assertFalse(status["change_required"])
                self.assertTrue(status["applied_on_restart"])
                self.assertFalse(runtime_config.roots_restart_required())

    def test_saved_root_marks_a_restart_and_survives_symlinked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-projects"
            real.mkdir()
            link = Path(tmp) / "linked-projects"
            link.symlink_to(real, target_is_directory=True)
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            for stale in ("QUIRQ_HOST_PROJECTS_ROOT", "QUIRQ_HOST_STATE_ROOT"):
                os.environ.pop(stale, None)
            env = {
                "XO_PROJECTS_ROOT": str(link),
                "QUIRQ_STATE_ROOT": str(state_root),
            }
            with patch.dict(os.environ, env, clear=False):
                # Saving the symlinked spelling of the root in use is not a
                # change: xo_projects_root() resolves, the Setup field does not.
                runtime_config.save_root_settings(
                    {
                        "xo_projects_root": str(link),
                        "quirq_state_root": str(state_root),
                    }
                )
                self.assertFalse(runtime_config.root_settings()["change_required"])
                self.assertNotIn("roots", runtime_config.restart_reasons())

                # A genuinely different root is a restart reason, because
                # startup reads roots.env (see server.py).
                elsewhere = Path(tmp) / "other-projects"
                runtime_config.save_root_settings(
                    {
                        "xo_projects_root": str(elsewhere),
                        "quirq_state_root": str(state_root),
                    }
                )
                status = runtime_config.root_settings()
                self.assertTrue(status["change_required"])
                self.assertEqual(status["configured"]["xo_projects_root"], str(elsewhere))
                self.assertIn("roots", runtime_config.restart_reasons())

    def test_watcher_can_load_every_manifest_source(self) -> None:
        manifests = [
            SimpleNamespace(name="first"),
            SimpleNamespace(name="second"),
        ]

        def module_for(_capability: str, *, agent: str):
            class FakeSource:
                name = agent

                def __init__(self, offsets) -> None:
                    self.offsets = offsets

            return SimpleNamespace(Source=FakeSource)

        with (
            patch.dict(os.environ, {"QUIRQ_WATCHER_SOURCE_MODE": "all"}),
            patch.object(watcher, "all_agents", return_value=manifests),
            patch.object(watcher, "try_load_capability", side_effect=module_for),
        ):
            instance = watcher.Watcher()

        self.assertEqual(
            [source.name for source in instance.sources],
            ["first", "second"],
        )


if __name__ == "__main__":
    unittest.main()
