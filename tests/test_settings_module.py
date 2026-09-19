"""The settings module: what ``modules/settings/`` declares and answers.

``tests/test_modules.py`` holds every module to the general contract; this
file pins the settings-specific facts:

* the manifest declares api, commands and the two pages (Modules and
  Workspace), owns the ``settings/`` folder and lists the three legacy route
  prefixes as aliases;
* the routes answer the same paths the four routers they came from did
  (runtime config, the curated and the legacy secrets routes, onboarding),
  with the same bodies and the same typed refusals; the old import paths
  (three services, four routers, the scope in ``scopes.py``) resolve to the
  moved objects;
* ``FILES`` matches the sample state root both ways under ``settings/`` and
  ``secrets/``, and the onboarding document follows the Document rules (a
  corrupt file is refused, never rewritten; unknown keys survive);
* the Workspace page spec validates, reads a served route, its forms submit
  to served routes with exactly the fields those routes accept, and the
  payload answers every path the blocks name;
* the commands answer through the facade and the registry.

Hermetic: the sample state root per test, the secret store and the
onboarding document pointed inside it, and one installed agent named like
the sample's, so no real home is read.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from modules.settings import commands, routes, runtime_config, service, store, xo_cowork_state
from services import modules as registry
from services.cowork_agent.registry import agent_env
from services.errors import ServiceError
from tests.support import ROOT, STATE_FIXTURE, Sandbox, client

PAGES = ROOT / "modules" / "settings" / "pages"
PAGE_SCHEMA = ROOT / "services" / "schema" / "page.schema.json"
MASK = "••••••"
SAMPLE_KEY, SAMPLE_VALUE = "EXAMPLE_API_KEY", "replace-with-your-key"

#: Every (method, path) the four routers served; the module serves exactly these.
EXPECTED_ROUTES = {
    ("GET", "/api/runtime-config"), ("PUT", "/api/runtime-config"),
    ("PUT", "/api/runtime-config/roots"), ("POST", "/api/runtime-config/restart"),
    ("GET", "/api/secrets"), ("PUT", "/api/secrets"), ("GET", "/api/secrets/{key}/reveal"),
    ("PATCH", "/api/secrets/{key}"), ("DELETE", "/api/secrets/{key}"),
    ("GET", "/api/secrets/env"), ("PUT", "/api/secrets/env"), ("GET", "/api/secrets/env/keys"),
    ("GET", "/api/onboarding"), ("POST", "/api/onboarding/complete"),
}


def _served() -> set[tuple[str, str]]:
    return {(m, getattr(r, "path", "")) for r in routes.router.routes for m in getattr(r, "methods", ())}


def _manifest(name: str, home: Path) -> SimpleNamespace:
    return SimpleNamespace(name=name, binary=f"{name}-bin", home_dir=home,
                           raw={"runtime_setup": {"session_globs": ["sessions/*.jsonl"], "secrets": []}})


def _path(expr: str) -> str:
    """The path part of an expression: what comes before the first pipe."""
    return expr.split("|", 1)[0].strip()


def _lookup(source: Any, path: str) -> tuple[bool, Any]:
    """``(found, value)`` walking a dotted path the way ``js/core/expr.js`` does
    (``page`` is the payload itself)."""
    if path in ("", ".", "page"):
        return True, source
    if path.startswith("page."):
        path = path[5:]
    value = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


class _SettingsCase(unittest.TestCase):
    """The sample state root, the secret store and the onboarding document
    pointed inside it, and one installed agent named like the sample's."""

    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        self.settings = self.sandbox.state / "settings"
        self.secrets_file = self.sandbox.state / "secrets" / "secrets.env"
        self.agent = _manifest("sample_agent", self.sandbox.base / "agent-home")
        for patcher in (
            patch.object(agent_env, "ENV_FILE", self.secrets_file),
            patch.object(xo_cowork_state, "STATE_DIR", self.settings),
            patch.object(xo_cowork_state, "STATE_FILE", self.settings / "onboarding.json"),
            patch.object(xo_cowork_state, "LEGACY_STATE_FILE", self.sandbox.base / "legacy" / "state.json"),
            patch.object(runtime_config, "all_agents", return_value=[self.agent]),
            patch.object(runtime_config, "get_active_agent", return_value=self.agent),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = client(routes.router)

    def secrets_text(self) -> str:
        return self.secrets_file.read_text(encoding="utf-8")


# ── The manifest and the moved objects ───────────────────────────────────────


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("settings")
        self.assertEqual(module.folder, "settings")
        for kind in ("api", "commands", "pages"):
            self.assertTrue(module.declares(kind), kind)
            self.assertTrue(registry.implements("settings", kind), kind)
        for kind in ("stream", "tasks", "listeners"):
            self.assertFalse(module.declares(kind), kind)
        self.assertEqual(sorted(p.id for p in registry.pages("settings")), ["modules", "workspace"])
        self.assertEqual(set(module.aliases), {"/api/runtime-config", "/api/secrets", "/api/onboarding"})
        self.assertTrue(module.description)

    def test_every_route_sits_under_the_aliases(self) -> None:
        module = registry.get("settings")
        allowed = ("/api/settings",) + module.aliases
        for route in routes.router.routes:
            path = getattr(route, "path", "")
            self.assertTrue(any(path == a or path.startswith(a + "/") for a in allowed), path)

    def test_the_routes_answer_the_same_paths_the_four_routers_did(self) -> None:
        self.assertEqual(_served(), EXPECTED_ROUTES)

    def test_the_old_import_paths_resolve_to_the_moved_objects(self) -> None:
        from routers.cowork_agent import onboarding as old_onboarding
        from routers.cowork_agent import runtime_config as old_runtime_router
        from routers.cowork_agent import secrets as old_secrets
        from routers.cowork_agent.bff import secrets as old_bff_secrets
        from services import setup_status as old_setup_status
        from services.cowork_agent import runtime_config as old_runtime_config
        from services.cowork_agent import scopes
        from services.cowork_agent import xo_cowork_state as old_state

        import modules.settings.setup_status as new_setup_status

        self.assertIs(old_runtime_config, runtime_config)
        self.assertIs(old_state, xo_cowork_state)
        self.assertIs(old_setup_status, new_setup_status)
        for old in (old_onboarding, old_runtime_router, old_secrets, old_bff_secrets):
            self.assertIs(old, routes)
            self.assertIs(old.router, routes.router)
        self.assertIs(scopes.SecretsScope, service.SecretsScope)
        self.assertIsInstance(scopes.resolve_scope("secrets"), service.SecretsScope)

    def test_the_hidden_keys_are_the_runtime_controls(self) -> None:
        from routers.cowork_agent.bff import filters

        self.assertEqual(service.HIDDEN_KEYS, runtime_config.RUNTIME_CONFIG_KEYS)
        self.assertEqual(service.HIDDEN_KEYS, filters.HIDDEN_KEYS, "the BFF filters state the same rule")
        self.assertEqual(service.preview_value("anything"), filters.preview_value("anything"))


# ── The file table ───────────────────────────────────────────────────────────


class FileTableTests(unittest.TestCase):
    def test_files_match_the_sample_both_ways(self) -> None:
        for folder in ("settings", "secrets"):
            examples = {p.relative_to(STATE_FIXTURE).as_posix()
                        for p in (STATE_FIXTURE / folder).rglob("*") if p.is_file()}
            declared = [spec for spec in store.FILES if spec.folder == folder]
            self.assertTrue(examples and declared, folder)
            for spec in declared:
                with self.subTest(pattern=spec.pattern):
                    self.assertTrue(any(spec.matches(e) for e in examples), f"no example for {spec.pattern}")
            for example in examples:
                with self.subTest(example=example):
                    self.assertTrue(any(spec.matches(example) for spec in declared), f"{example} is undeclared")

    def test_roles_and_the_kernel_file(self) -> None:
        roles = {spec.pattern: spec.role for spec in store.FILES}
        self.assertEqual({p for p, r in roles.items() if r == "secret"}, {"secrets/secrets.env", "secrets/token.json"})
        self.assertTrue(all(r == "decision" for p, r in roles.items() if p.startswith("settings/")))
        modules_file = next(spec for spec in store.FILES if spec.pattern == "settings/modules.json")
        self.assertIn("kernel", modules_file.note or "")
        self.assertEqual(store.modules_file(), registry.settings_path())

    def test_the_onboarding_document(self) -> None:
        document = store.onboarding_document()
        self.assertEqual((document.name, document.schema, document.path), ("onboarding.json", 1, store.onboarding_file()))
        self.assertEqual(store.onboarding_document(Path("/elsewhere/onboarding.json")).path, Path("/elsewhere/onboarding.json"))
        self.assertEqual(xo_cowork_state.STATE_SCHEMA, store.ONBOARDING_SCHEMA)


# ── Runtime settings and roots ───────────────────────────────────────────────


class RuntimeRouteTests(_SettingsCase):
    def test_get_answers_the_status_payload(self) -> None:
        response = self.client.get("/api/runtime-config")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        for key in ("configured", "applied", "restart_required", "restart_reasons", "restart_mode",
                    "restart_supported", "roots", "agents", "paths", "network"):
            self.assertIn(key, payload)
        self.assertEqual(payload["applied"]["agent_name"], "sample_agent")
        # the sample runtime.env is what is configured
        self.assertEqual(payload["configured"], {"agent_name": "sample_agent", "watcher_enabled": True,
                                                 "watcher_interval_seconds": 1.0, "watcher_source_mode": "all"})
        self.assertEqual([a["name"] for a in payload["agents"]], ["sample_agent"])
        self.assertIsInstance(payload["restart_required"], bool)
        self.assertEqual(payload["roots"]["applied"]["quirq_state_root"], str(self.sandbox.state))

    def test_put_saves_runtime_env_and_refuses_a_bad_body_with_the_bare_message(self) -> None:
        body = {"agent_name": "sample_agent", "watcher_enabled": False,
                "watcher_interval_seconds": 2.5, "watcher_source_mode": "active"}
        response = self.client.put("/api/runtime-config", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["saved"], body)
        self.assertEqual(response.json()["status"]["configured"], body)
        runtime_file = self.settings / "runtime.env"
        self.assertEqual(stat.S_IMODE(runtime_file.stat().st_mode), 0o600)
        self.assertIn("QUIRQ_WATCHER_INTERVAL_SECONDS=2.5", runtime_file.read_text(encoding="utf-8"))
        refused = self.client.put("/api/runtime-config", json={**body, "agent_name": "ghost"})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json(), {"detail": "Unknown agent backend 'ghost'. Available: sample_agent"})
        self.assertEqual(self.client.put("/api/runtime-config", json={"agent_name": "sample_agent"}).status_code, 422)

    def test_put_roots_saves_roots_env_and_refuses_a_relative_path(self) -> None:
        body = {"xo_projects_root": str(self.sandbox.projects), "quirq_state_root": str(self.sandbox.state)}
        response = self.client.put("/api/runtime-config/roots", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved"], body)
        self.assertFalse(response.json()["status"]["roots"]["change_required"])
        self.assertIn(f"XO_PROJECTS_ROOT={self.sandbox.projects}", (self.settings / "roots.env").read_text(encoding="utf-8"))
        refused = self.client.put("/api/runtime-config/roots", json={**body, "xo_projects_root": "relative"})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json(), {"detail": "XO root must be an absolute host path"})

    def test_the_facade_refuses_with_a_codeless_400_and_delegates_at_call_time(self) -> None:
        with self.assertRaises(ServiceError) as caught:
            service.save_settings({"agent_name": "ghost", "watcher_enabled": True,
                                   "watcher_interval_seconds": 1, "watcher_source_mode": "all"})
        self.assertEqual((caught.exception.code, caught.exception.status), (None, 400))
        with patch.object(runtime_config, "restart_mode", return_value="managed"):
            self.assertEqual(service.restart_mode(), "managed")
        overview = service.overview()
        self.assertEqual(overview["effective"]["agent_name"], "sample_agent")
        self.assertEqual(set(overview), {"effective", "saved", "restart_required", "restart_reasons", "restart_mode"})


# ── Secrets ──────────────────────────────────────────────────────────────────


class SecretsRouteTests(_SettingsCase):
    def test_the_listing_masks_values(self) -> None:
        response = self.client.get("/api/secrets")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": [{"key": SAMPLE_KEY, "is_set": True, "preview": MASK}], "total": 1})
        self.assertNotIn(SAMPLE_VALUE, response.text)

    def test_reveal_answers_one_key_and_hides_the_runtime_controls(self) -> None:
        self.assertEqual(self.client.get(f"/api/secrets/{SAMPLE_KEY}/reveal").json(), {"key": SAMPLE_KEY, "value": SAMPLE_VALUE})
        missing = self.client.get("/api/secrets/NOPE/reveal")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "key_not_found"))
        hidden = self.client.get("/api/secrets/AGENT_NAME/reveal")
        self.assertEqual((hidden.status_code, hidden.json()["detail"]["code"]), (404, "key_not_found"))
        invalid = self.client.get("/api/secrets/lower/reveal")
        self.assertEqual((invalid.status_code, invalid.json()["detail"]["code"]), (400, "invalid_key"))

    def test_patch_edits_one_line_and_delete_is_idempotent(self) -> None:
        response = self.client.patch("/api/secrets/QUIRQ_TEST_SECRET", json={"value": "shh"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"key": "QUIRQ_TEST_SECRET", "is_set": True, "preview": MASK})
        self.assertIn("QUIRQ_TEST_SECRET=shh", self.secrets_text())
        self.assertIn(f"{SAMPLE_KEY}={SAMPLE_VALUE}", self.secrets_text(), "a line-level edit keeps the rest")
        self.assertEqual(stat.S_IMODE(self.secrets_file.stat().st_mode), 0o600)
        reserved = self.client.patch("/api/secrets/AGENT_NAME", json={"value": "x"})
        self.assertEqual((reserved.status_code, reserved.json()["detail"]["code"]), (400, "invalid_key"))
        bad_value = self.client.patch("/api/secrets/QUIRQ_TEST_SECRET", json={"value": "a\nb"})
        self.assertEqual((bad_value.status_code, bad_value.json()["detail"]["code"]), (400, "invalid_value"))
        self.assertEqual(self.client.patch("/api/secrets/QUIRQ_TEST_EMPTY", json={"value": ""}).json(),
                         {"key": "QUIRQ_TEST_EMPTY", "is_set": False, "preview": None})
        self.assertEqual(self.client.delete("/api/secrets/QUIRQ_TEST_SECRET").json(), {"key": "QUIRQ_TEST_SECRET", "deleted": True})
        self.assertEqual(self.client.delete("/api/secrets/QUIRQ_TEST_SECRET").json(), {"key": "QUIRQ_TEST_SECRET", "deleted": False})
        self.assertEqual(self.client.delete("/api/secrets/AGENT_NAME").json(), {"key": "AGENT_NAME", "deleted": False})
        self.assertNotIn("QUIRQ_TEST_SECRET", self.secrets_text())

    def test_put_replaces_the_store_and_refuses_duplicates(self) -> None:
        duplicate = self.client.put("/api/secrets", json={"items": [{"key": "QUIRQ_TEST_ONE", "value": "1"},
                                                                     {"key": "QUIRQ_TEST_ONE", "value": "2"}]})
        self.assertEqual((duplicate.status_code, duplicate.json()["detail"]["code"]), (400, "duplicate_key"))
        self.assertIn(SAMPLE_KEY, self.secrets_text(), "a refused body writes nothing")
        response = self.client.put("/api/secrets", json={"items": [{"key": "QUIRQ_TEST_ONE", "value": "1"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": [{"key": "QUIRQ_TEST_ONE", "is_set": True, "preview": MASK}], "total": 1})
        self.assertEqual(self.secrets_text(), "QUIRQ_TEST_ONE=1\n")

    def test_the_legacy_whole_file_view(self) -> None:
        self.assertEqual(self.client.get("/api/secrets/env").json(), {"entries": [{"key": SAMPLE_KEY, "value": SAMPLE_VALUE}]})
        self.assertEqual(self.client.get("/api/secrets/env/keys").json(), {"keys": [SAMPLE_KEY]})
        response = self.client.put("/api/secrets/env", json={"entries": [{"key": "QUIRQ_TEST_TWO", "value": "2"},
                                                                          {"key": "QUIRQ_TEST_BLANK", "value": ""}]})
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.client.get("/api/secrets/env/keys").json(), {"keys": ["QUIRQ_TEST_TWO"]})
        self.assertEqual(self.client.put("/api/secrets/env", json=["not", "an", "object"]).status_code, 400)

    def test_a_store_that_cannot_be_read_is_a_500_scope_unavailable(self) -> None:
        with patch.object(agent_env, "load_env_entries", side_effect=OSError("disk")):
            response = self.client.get("/api/secrets")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (500, "scope_unavailable"))
        self.assertNotIn("disk", response.text)


# ── Onboarding ───────────────────────────────────────────────────────────────


class OnboardingRouteTests(_SettingsCase):
    def test_status_reads_the_sample(self) -> None:
        self.assertEqual(self.client.get("/api/onboarding").json(),
                         {"completed": True, "completed_at": "2025-12-01T10:00:00Z"})

    def test_complete_writes_the_document_and_keeps_unknown_keys(self) -> None:
        onboarding = self.settings / "onboarding.json"
        onboarding.write_text(json.dumps({"schema": 1, "custom": "kept", "onboarding_completed": False}), encoding="utf-8")
        self.assertEqual(self.client.get("/api/onboarding").json(), {"completed": False, "completed_at": None})
        self.assertEqual(self.client.post("/api/onboarding/complete").json(), {"ok": True})
        status = self.client.get("/api/onboarding").json()
        self.assertTrue(status["completed"])
        self.assertTrue(status["completed_at"].endswith("Z"))
        written = json.loads(onboarding.read_text(encoding="utf-8"))
        self.assertEqual((written["schema"], written["custom"], written["onboarding_completed"]), (1, "kept", True))
        self.assertNotIn("updated_at", written, "the document has always carried only its own keys")

    def test_an_absent_document_reads_as_not_completed(self) -> None:
        (self.settings / "onboarding.json").unlink()
        self.assertEqual(self.client.get("/api/onboarding").json(), {"completed": False, "completed_at": None})
        self.assertFalse((self.settings / "onboarding.json").exists(), "a read creates nothing")

    def test_a_corrupt_document_is_refused_and_never_rewritten(self) -> None:
        onboarding = self.settings / "onboarding.json"
        onboarding.write_text("not json", encoding="utf-8")
        self.assertEqual(self.client.get("/api/onboarding").json(), {"completed": False, "completed_at": None})
        response = self.client.post("/api/onboarding/complete")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "corrupt_document"))
        self.assertNotIn(str(onboarding), response.text)
        self.assertEqual(onboarding.read_text(encoding="utf-8"), "not json")

    def test_the_pre_rename_state_is_adopted_once_without_rewriting_it(self) -> None:
        (self.settings / "onboarding.json").unlink()
        legacy = xo_cowork_state.LEGACY_STATE_FILE
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"onboarding_completed": True, "onboarding_completed_at": "2026-07-25T00:00:00Z"}), encoding="utf-8")
        self.assertEqual(self.client.get("/api/onboarding").json(), {"completed": True, "completed_at": "2026-07-25T00:00:00Z"})
        adopted = json.loads((self.settings / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(adopted, {"onboarding_completed": True, "onboarding_completed_at": "2026-07-25T00:00:00Z", "schema": 1})
        self.assertNotIn("schema", json.loads(legacy.read_text(encoding="utf-8")))


# ── The Workspace page ───────────────────────────────────────────────────────


class WorkspacePageTests(_SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.spec = json.loads((PAGES / "workspace.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_spec_validates_against_the_page_schema(self) -> None:
        import jsonschema

        schema = json.loads(PAGE_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft7Validator(schema).validate(self.spec)
        self.assertEqual((self.spec["id"], self.spec["tab"], self.spec["route"], self.spec["order"]),
                         ("workspace", "setup", "setup/workspace", 10))
        self.assertIn(self.spec["tab"], {t["id"] for t in registry.tabs()})
        self.assertEqual(next(t["default"] for t in registry.tabs() if t["id"] == "setup"), "setup/workspace")

    def test_the_read_and_every_submit_are_served_with_the_fields_the_routes_accept(self) -> None:
        served = _served()
        self.assertIn(("GET", self.spec["read"]), served)
        forms = {b["submit"]: b for b in self.spec["blocks"] if b["type"] == "form"}
        self.assertEqual(set(forms), {"PUT /api/runtime-config", "PUT /api/runtime-config/roots"})
        models = {"PUT /api/runtime-config": routes.RuntimeConfigRequest, "PUT /api/runtime-config/roots": routes.RootConfigRequest}
        for submit, block in forms.items():
            method, path = submit.split(" ", 1)
            self.assertIn((method, path), served)
            self.assertEqual({f["name"] for f in block["fields"]}, set(models[submit].model_fields), submit)
        by_name = {f["name"]: f for f in forms["PUT /api/runtime-config"]["fields"]}
        self.assertEqual(by_name["watcher_enabled"]["type"], "toggle")
        self.assertEqual((by_name["watcher_interval_seconds"]["type"], by_name["watcher_interval_seconds"]["min"],
                          by_name["watcher_interval_seconds"]["max"]), ("number", 0.25, 60))
        self.assertEqual({o["value"] for o in by_name["watcher_source_mode"]["options"]}, {"active", "all"})
        self.assertTrue(any(b["type"] == "text" and b.get("when") == "restart_required" for b in self.spec["blocks"]))

    def test_the_payload_answers_every_path_the_blocks_name(self) -> None:
        payload = self.client.get(self.spec["read"]).json()
        for block in self.spec["blocks"]:
            with self.subTest(block=block.get("title") or block["type"]):
                if "when" in block:
                    self.assertTrue(_lookup(payload, _path(block["when"]))[0], block["when"])
                if isinstance(block.get("text"), str):
                    self.assertTrue(_lookup(payload, _path(block["text"]))[0], block["text"])
                for field in block.get("fields", []):
                    self.assertTrue(_lookup(payload, _path(field["value"]))[0], field["value"])
                if block["type"] == "detail":
                    found, target = _lookup(payload, _path(block.get("items", "")))
                    self.assertTrue(found and isinstance(target, dict), block.get("items"))
                    for column in block["columns"]:
                        self.assertTrue(_lookup(target, _path(column["value"]))[0], column["value"])
                if block["type"] == "list":
                    found, rows = _lookup(payload, _path(block["items"]))
                    self.assertTrue(found and rows, block["items"])
                    row_spec = block["row"]
                    fields = [row_spec["title"], row_spec["detail"], row_spec["badge"]["value"], row_spec["tone"]["value"], block["key"]]
                    fields += list(row_spec["meta"])
                    for row in rows:
                        for expr in fields:
                            self.assertTrue(_lookup(row, _path(expr))[0], f"row lacks {expr!r}")

    def test_the_ui_lists_both_pages_while_the_module_is_on(self) -> None:
        pages = {(p["module"], p["id"]): p for p in registry.ui()["pages"]}
        self.assertIn(("settings", "modules"), pages)
        self.assertEqual(pages[("settings", "workspace")]["route"], "setup/workspace")
        self.assertEqual(pages[("settings", "workspace")]["spec"]["read"], "/api/runtime-config")
        registry.override("settings", {"pages": {"workspace": False}})
        self.assertNotIn(("settings", "workspace"), {(p["module"], p["id"]) for p in registry.ui()["pages"]})


# ── Commands ─────────────────────────────────────────────────────────────────


class CommandTests(_SettingsCase):
    def test_settings_and_modules_answer_through_the_facade_and_the_registry(self) -> None:
        self.assertEqual(set(commands.COMMANDS), {"settings", "modules"})
        self.assertEqual(set(registry.commands()["settings"]), {"settings", "modules"})
        settings = commands.COMMANDS["settings"]([])
        self.assertEqual(settings["effective"]["agent_name"], "sample_agent")
        self.assertEqual(settings["saved"]["watcher_source_mode"], "all")
        self.assertIsInstance(settings["restart_required"], bool)
        table = commands.COMMANDS["modules"]([])
        self.assertEqual(table["schema"], 1)
        self.assertIn("settings", [m["name"] for m in table["modules"]])
        self.assertTrue(os.path.basename(table["settings_file"]) == "modules.json")


if __name__ == "__main__":
    unittest.main()
