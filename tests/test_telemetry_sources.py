"""Telemetry source configuration: descriptors, saving paths and the
per-source collection switch the Agents tab's Configure page drives."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import telemetry_sources
from services.cowork_agent.visualizer import session_telemetry


class FakeStore:
    """The secrets scope handle: a dict with upsert/delete semantics."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def upsert(self, key: str, value: str) -> None:
        self.entries[key] = value

    def delete(self, key: str) -> bool:
        return self.entries.pop(key, None) is not None


def _provider(source_id: str, *, config: dict | None = None, label: str | None = None):
    return SimpleNamespace(
        SOURCE_ID=source_id,
        SOURCE_LABEL=label or source_id.title(),
        COST_STATUS="unavailable",
        SOURCE_CONFIG=config,
        collect_session_telemetry=lambda: {
            "source": {"id": source_id, "label": label or source_id.title(), "cost_status": "unavailable"},
            "meta_priority": 1,
            "meta": {},
            "totals": {"sessions": 1, "tokens": 5, "cost_usd": 0.0},
            "project_keys": [],
            "sessions": [{"id": "s", "key": source_id + ":s", "agent": source_id, "started_at": "2026-01-01T00:00:00Z"}],
            "daily_models": [],
            "daily_sessions": [],
            "daily_tools": [],
        },
    )


class DescribeSourceTests(unittest.TestCase):
    def test_descriptor_reports_effective_path_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            provider = _provider("alpha", config={
                "vendor": "anthropic",
                "path_env": "ALPHA_HOME",
                "path_default": "~/.alpha",
                "path_kind": "dir",
                "collects": ["sessions"],
            })
            with mock.patch.dict(os.environ, {"ALPHA_HOME": str(home)}, clear=False):
                row = telemetry_sources.describe_source("alpha", provider)
            self.assertEqual(row["vendor"], "anthropic")
            self.assertTrue(row["enabled"])
            self.assertEqual(row["path"]["env"], "ALPHA_HOME")
            self.assertEqual(row["path"]["configured"], str(home))
            self.assertEqual(row["path"]["effective"], str(home))
            self.assertTrue(row["path"]["exists"])
            self.assertTrue(row["path"]["is_expected_kind"])
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ALPHA_HOME", None)
                row = telemetry_sources.describe_source("alpha", provider)
            self.assertEqual(row["path"]["configured"], "")
            self.assertEqual(row["path"]["effective"], "~/.alpha")

    def test_provider_without_config_is_listed_read_only(self) -> None:
        row = telemetry_sources.describe_source("bare", _provider("bare"))
        self.assertEqual(row["vendor"], "unknown")
        self.assertFalse(row["path"]["editable"])
        self.assertIsNone(row["path"]["env"])
        self.assertFalse(row["path"]["exists"])


class SaveSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.provider = _provider("alpha", config={
            "vendor": "openai", "path_env": "ALPHA_HOME", "path_default": "~/.alpha",
        })
        patches = [
            mock.patch.object(telemetry_sources, "_load_providers", return_value=[("alpha", self.provider)]),
            mock.patch.object(telemetry_sources.scopes, "resolve_scope", return_value=self.store),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("ALPHA_HOME", None)
        os.environ.pop(telemetry_sources.DISABLED_ENV, None)

    def test_path_is_saved_to_the_store_and_the_process_env(self) -> None:
        row = telemetry_sources.save_source("alpha", path="  ~/elsewhere/.alpha ")
        self.assertEqual(self.store.entries["ALPHA_HOME"], "~/elsewhere/.alpha")
        self.assertEqual(os.environ["ALPHA_HOME"], "~/elsewhere/.alpha")
        self.assertEqual(row["path"]["configured"], "~/elsewhere/.alpha")

    def test_empty_path_clears_the_override(self) -> None:
        telemetry_sources.save_source("alpha", path="/tmp/x")
        row = telemetry_sources.save_source("alpha", path="")
        self.assertNotIn("ALPHA_HOME", self.store.entries)
        self.assertNotIn("ALPHA_HOME", os.environ)
        self.assertEqual(row["path"]["effective"], "~/.alpha")

    def test_relative_and_multiline_paths_are_rejected(self) -> None:
        for bad in ("relative/path", "x\ny"):
            with self.assertRaises(telemetry_sources.InvalidTelemetryPath):
                telemetry_sources.save_source("alpha", path=bad)
        self.assertEqual(self.store.entries, {})

    def test_switch_writes_the_disabled_list(self) -> None:
        row = telemetry_sources.save_source("alpha", enabled=False)
        self.assertFalse(row["enabled"])
        self.assertEqual(self.store.entries[telemetry_sources.DISABLED_ENV], "alpha")
        row = telemetry_sources.save_source("alpha", enabled=True)
        self.assertTrue(row["enabled"])
        self.assertNotIn(telemetry_sources.DISABLED_ENV, self.store.entries)
        self.assertNotIn(telemetry_sources.DISABLED_ENV, os.environ)

    def test_unknown_source_is_a_404(self) -> None:
        with self.assertRaises(telemetry_sources.UnknownTelemetrySource) as ctx:
            telemetry_sources.save_source("nope", path="/tmp")
        self.assertEqual(ctx.exception.status, 404)


class BuilderSwitchTests(unittest.TestCase):
    def test_disabled_source_is_listed_but_never_read(self) -> None:
        on, off = _provider("on"), _provider("off")
        off.collect_session_telemetry = mock.Mock(side_effect=AssertionError("must not read"))
        with mock.patch.object(
            session_telemetry, "list_capability_providers", return_value=["off", "on"]
        ), mock.patch.object(
            session_telemetry, "try_load_capability", side_effect=lambda _c, *, agent: {"on": on, "off": off}[agent]
        ), mock.patch.dict(os.environ, {telemetry_sources.DISABLED_ENV: "off"}, clear=False):
            data = session_telemetry.build_session_telemetry()
        by_id = {row["id"]: row for row in data["meta"]["sources"]}
        self.assertEqual(by_id["off"]["status"], "disabled")
        self.assertFalse(by_id["off"]["available"])
        self.assertEqual(by_id["on"]["status"], "available")
        off.collect_session_telemetry.assert_not_called()


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        from routers.telemetry_sources import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_list_and_update_shape(self) -> None:
        provider = _provider("alpha", config={"vendor": "cursor", "path_env": "ALPHA_HOME", "path_default": "~/.alpha"})
        store = FakeStore()
        with mock.patch.object(
            telemetry_sources, "_load_providers", return_value=[("alpha", provider)]
        ), mock.patch.object(
            telemetry_sources.scopes, "resolve_scope", return_value=store
        ), mock.patch("routers.telemetry_sources._rebuild_sessions_view") as rebuild, mock.patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("ALPHA_HOME", None)
            listed = self.client.get("/api/telemetry/sources")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["total"], 1)
            self.assertEqual(listed.headers["cache-control"], "no-store")
            saved = self.client.put("/api/telemetry/sources/alpha", json={"path": "/data/alpha"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["item"]["path"]["configured"], "/data/alpha")
            self.assertTrue(saved.json()["rebuilding"])
            self.client.get("/api/telemetry/sources")  # let the executor run
        rebuild.assert_called()
        bad = self.client.put("/api/telemetry/sources/alpha", json={"path": "x", "extra": 1})
        self.assertEqual(bad.status_code, 422)
        with mock.patch.object(telemetry_sources, "_load_providers", return_value=[]):
            missing = self.client.put("/api/telemetry/sources/ghost", json={"enabled": False})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "unknown_source")


if __name__ == "__main__":
    unittest.main()
