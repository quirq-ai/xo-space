"""Deployment lifecycle and filesystem boundary tests for the Fly catalog."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.adapters.fly import agents, artifacts, models, paths


def fixture(*, identity="fern", revision=1, digest="a" * 64) -> dict:
    # Catalog tests mock only the independently tested shared-core validator.
    return {
        "fly": {"id": identity, "name": "Fern", "revision": revision},
        "integrity": {"digest": digest}, "taskFamily": "report-scout-v1",
        "task": {"fields": ["run", "success"], "maxReads": 4},
        "policy": {"trainedEpochs": 60},
    }


class FlyCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.environment = patch.dict("os.environ", {"QUIRQ_STATE_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.inputs = paths.flies_dir()
        self.inputs.mkdir(parents=True)

        async def validate(payload):
            value = payload["artifact"]
            if "fly" not in value or value.get("invalid"):
                raise ValueError("Invalid portable Fly artifact.")
            return {"artifact": copy.deepcopy(value)}

        self.validator = AsyncMock(side_effect=validate)
        self.validation = patch.object(artifacts, "node_call", self.validator)
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def deploy(self, value=None, filename="fern.fly.json"):
        path = self.inputs / filename
        path.write_text(json.dumps(value or fixture()), encoding="utf-8")
        return path

    async def test_discovery_validation_selection_and_immutable_checkpoint(self):
        self.deploy()
        result = await artifacts.reload_catalog()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["flies"][0]["model"], "fly/fern")
        selected = await artifacts.resolve_artifact("fly/fern")
        self.assertEqual(selected["artifact"], fixture())
        self.assertEqual(await artifacts.load_snapshot("a" * 64), fixture())
        self.assertTrue((paths.versions_dir() / ("a" * 64 + ".json")).is_file())
        self.assertTrue(self.validator.await_count >= 4)
        self.assertEqual(json.loads(paths.catalog_path().read_text())["schema"], 1)

    async def test_nested_and_wrong_extension_files_are_not_deployment_inputs(self):
        self.deploy(filename="ignored.json")
        nested = self.inputs / "nested"
        nested.mkdir()
        (nested / "hidden.fly.json").write_text(json.dumps(fixture()))
        self.assertEqual(await artifacts.list_flies(), [])
        self.validator.assert_not_awaited()

    async def test_duplicate_ids_are_quarantined_instead_of_arbitrarily_selected(self):
        self.deploy(filename="a.fly.json")
        self.deploy(filename="b.fly.json")
        result = await artifacts.reload_catalog()
        self.assertEqual(result["flies"], [])
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all("Duplicate" in error["error"] for error in result["errors"]))

    async def test_malformed_replacement_preserves_previously_accepted_checkpoint(self):
        deployed = self.deploy()
        await artifacts.reload_catalog()
        deployed.write_text("{truncated")
        result = await artifacts.reload_catalog()
        self.assertEqual(result["flies"][0]["digest"], "a" * 64)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(await artifacts.load_snapshot("a" * 64), fixture())

    async def test_revisions_require_increment_and_existing_sessions_keep_previous(self):
        deployed = self.deploy()
        await artifacts.reload_catalog()
        self.deploy(fixture(digest="b" * 64))
        denied = await artifacts.reload_catalog()
        self.assertEqual(denied["flies"][0]["digest"], "a" * 64)
        self.assertIn("higher revision", denied["errors"][0]["error"])
        self.deploy(fixture(revision=2, digest="b" * 64))
        self.assertEqual((await artifacts.resolve_artifact())["digest"], "b" * 64)
        self.assertEqual((await artifacts.load_snapshot("a" * 64))["fly"]["revision"], 1)
        deployed.unlink()
        self.assertEqual(await artifacts.list_flies(), [])
        self.assertEqual((await artifacts.load_snapshot("b" * 64))["fly"]["revision"], 2)

    async def test_symbolic_links_and_oversize_files_do_not_reach_validator(self):
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text(json.dumps(fixture()))
        (self.inputs / "linked.fly.json").symlink_to(outside)
        (self.inputs / "large.fly.json").write_bytes(b" " * (artifacts.MAX_ARTIFACT_BYTES + 1))
        result = await artifacts.reload_catalog()
        self.assertEqual(result["flies"], [])
        self.assertEqual(len(result["errors"]), 2)
        self.validator.assert_not_awaited()

    async def test_redirected_versions_directory_and_corrupt_snapshot_fail_closed(self):
        self.deploy()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        paths.versions_dir().symlink_to(outside, target_is_directory=True)
        self.assertEqual((await artifacts.reload_catalog())["flies"], [])
        self.assertEqual(list(outside.iterdir()), [])
        paths.versions_dir().unlink()
        await artifacts.reload_catalog()
        pinned = paths.versions_dir() / ("a" * 64 + ".json")
        pinned.write_text(json.dumps(fixture(digest="b" * 64)))
        with self.assertRaisesRegex(artifacts.FlyArtifactError, "does not match"):
            await artifacts.load_snapshot("a" * 64)
        self.assertEqual((await artifacts.reload_catalog())["flies"], [])

    async def test_selection_rejects_empty_unknown_ambiguous_and_pathlike_models(self):
        with self.assertRaisesRegex(artifacts.FlyArtifactError, "No valid"):
            await artifacts.resolve_artifact()
        self.deploy()
        self.deploy(fixture(identity="oak", digest="b" * 64), "oak.fly.json")
        with self.assertRaisesRegex(artifacts.FlyArtifactError, "explicit"):
            await artifacts.resolve_artifact()
        for value in ("../fern", "fly/../fern", "fern", "fly/absent"):
            with self.subTest(value=value), self.assertRaisesRegex(artifacts.FlyArtifactError, "Unknown"):
                await artifacts.resolve_artifact(value)
        with self.assertRaisesRegex(artifacts.FlyArtifactError, "digest"):
            await artifacts.load_snapshot("../outside")

    async def test_missing_runtime_becomes_actionable_catalog_error(self):
        self.deploy()
        self.validator.side_effect = RuntimeError("Fly requires Node.js 20 or newer; install Node or set FLY_NODE_PATH.")
        result = await artifacts.reload_catalog()
        self.assertEqual(result["flies"], [])
        self.assertIn("FLY_NODE_PATH", result["errors"][0]["error"])

    async def test_deployment_count_is_bounded(self):
        for index in range(artifacts.MAX_DEPLOYED_FLIES + 1):
            self.deploy(filename=f"{index}.fly.json")
        result = await artifacts.reload_catalog()
        self.assertEqual(result["flies"], [])
        self.assertIn("at most 100", result["errors"][0]["error"])
        self.validator.assert_not_awaited()


class FlyProfileTests(unittest.TestCase):
    def test_models_surface_artifacts_without_claiming_llm_features(self):
        metadata = artifacts._metadata(fixture(), "fern.fly.json")
        with patch.object(models, "list_flies", new=AsyncMock(return_value=[metadata])):
            listed = models.list_models()
        self.assertEqual(listed[0]["id"], "fly/fern")
        self.assertFalse(listed[0]["capabilities"]["reasoning"])
        self.assertFalse(listed[0]["metadata"]["token_billing"])
        with patch.object(models, "list_flies", new=AsyncMock(return_value=[])):
            self.assertEqual(models.list_models(), [])

    def test_agent_ids_remain_existing_project_ids(self):
        with patch.object(agents, "list_projects", return_value=[{
            "name": "space-a", "display_name": "My space", "path": "/tmp/projects/space-a",
        }]):
            listed = agents.list_agents()
        self.assertEqual(listed[0]["name"], "space-a")
        self.assertTrue(listed[0]["metadata"]["read_only"])
        self.assertEqual(agents.create_agent(None).status_code, 405)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for shared-core integration")
class FlyRealArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_export_roundtrip_and_tampered_replacement(self):
        exported = json.loads((Path(__file__).parent / "fixtures/fly/failure-scout.fly.json").read_text())
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"QUIRQ_STATE_ROOT": tmp}):
            paths.flies_dir().mkdir()
            deployed = paths.flies_dir() / "failure-scout.fly.json"
            deployed.write_text(json.dumps(exported))
            selected = await artifacts.resolve_artifact(f"fly/{exported['fly']['id']}")
            self.assertEqual(selected["artifact"], exported)
            changed = copy.deepcopy(exported)
            changed["policy"]["weights"][0] += 1
            deployed.write_text(json.dumps(changed))
            catalog = await artifacts.reload_catalog()
            self.assertEqual(catalog["flies"][0]["digest"], exported["integrity"]["digest"])
            self.assertTrue(catalog["errors"])
            self.assertEqual(await artifacts.load_snapshot(selected["digest"]), exported)


if __name__ == "__main__":
    unittest.main()
