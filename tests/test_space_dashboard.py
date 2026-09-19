from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import categorized_graph


ROOT = Path(__file__).resolve().parents[1]


class CategorizedGraphTests(unittest.TestCase):
    def test_classifier_uses_project_signals_and_allows_multiple_memberships(
        self,
    ) -> None:
        with patch.object(categorized_graph, "_saved_memberships", return_value=[]):
            memberships = categorized_graph.classify_project(
                "research-platform",
                [
                    "research-platform/package.json",
                    "research-platform/src/app.ts",
                    "research-platform/papers/results.ipynb",
                ],
            )

        self.assertEqual("research", memberships[0])
        self.assertIn("engineering", memberships)

    def test_manual_category_is_primary_and_old_aliases_are_supported(
        self,
    ) -> None:
        with patch.object(
            categorized_graph,
            "read_json",
            return_value={
                "category": "customer",
                "classification": {"categories": ["app", "docs"]},
            },
        ):
            memberships = categorized_graph.classify_project(
                "ambiguous",
                ["ambiguous/package.json", "ambiguous/README.md"],
            )

        self.assertEqual("marketing", memberships[0])
        self.assertIn("engineering", memberships)
        self.assertIn("documentation", memberships)

    def test_project_shape_is_independent_from_environment_membership(
        self,
    ) -> None:
        self.assertEqual(
            ("slab", "Slides"),
            categorized_graph._project_shape(["proposal/deck.pptx"]),
        )
        self.assertEqual(
            ("stack", "Docs"),
            categorized_graph._project_shape(
                ["handbook/README.md", "handbook/guide.md"]
            ),
        )
        self.assertEqual(
            ("disc", "App"),
            categorized_graph._project_shape(
                ["service/package.json", "service/src/app.ts"]
            ),
        )

    def test_builder_collapses_files_to_one_project_node(self) -> None:
        source = {
            "meta": {"workspace": "/tmp/xo-projects"},
            "hubs": [
                {"id": "p_app", "cat": "p_app", "label": "App"},
                {"id": "p_docs", "cat": "p_docs", "label": "Docs"},
            ],
            "groups": [
                {"id": "g_app_root", "cat": "p_app"},
                {"id": "g_docs_root", "cat": "p_docs"},
            ],
            "leaves": [
                {
                    "group": "g_app_root",
                    "path": "app/package.json",
                    "date": "2026-07-01",
                },
                {
                    "group": "g_app_root",
                    "path": "app/src/main.ts",
                    "date": "2026-07-02",
                },
                {
                    "group": "g_docs_root",
                    "path": "docs/README.md",
                    "date": "2026-07-03",
                },
                {
                    "group": "g_docs_root",
                    "path": "docs/guide.md",
                    "date": "2026-07-04",
                },
            ],
        }

        with (
            patch.object(
                categorized_graph, "build_space_data", return_value=source
            ),
            patch.object(
                categorized_graph, "_saved_memberships", return_value=[]
            ),
        ):
            payload = categorized_graph.build_categorized_graph()

        self.assertEqual(2, len(payload["leaves"]))
        self.assertEqual(5, len(payload["hubs"]))
        self.assertEqual("projects", payload["meta"]["noun"])
        self.assertEqual("environments", payload["meta"]["collectionLabel"])
        self.assertTrue(payload["meta"]["enclose"])
        self.assertEqual({"d": 80, "k": 0.07}, payload["meta"]["tieSpring"])
        self.assertEqual(5, len(payload["meta"]["shapeLegend"]))
        self.assertEqual("Environments", payload["root"]["label"])
        self.assertEqual(
            {
                "engineering",
                "ops",
                "documentation",
                "research",
                "marketing",
            },
            set(payload["categories"]),
        )
        by_id = {leaf["id"]: leaf for leaf in payload["leaves"]}
        self.assertEqual("g_engineering", by_id["app"]["group"])
        self.assertEqual("g_documentation", by_id["docs"]["group"])
        self.assertEqual(["engineering"], by_id["app"]["clusters"])
        self.assertEqual("output", by_id["app"]["xotype"])


class DashboardUiTests(unittest.TestCase):
    def test_dashboard_is_the_default_projects_lens_and_shares_the_graph_canvas(
        self,
    ) -> None:
        app = (ROOT / "space_ui" / "js" / "shell.js").read_text(encoding="utf-8")
        atlas = (
            ROOT / "space_ui" / "js" / "views" / "atlas.js"
        ).read_text(encoding="utf-8")
        registry = (
            ROOT / "space_ui" / "js" / "core" / "registry.js"
        ).read_text(encoding="utf-8")

        self.assertIn("registerView(dashboardView);", app)
        self.assertLess(
            app.index("registerView(dashboardView);"),
            app.index("registerView(graphView);"),
        )
        self.assertIn("startRegistry({tabs:PRIMARY_TABS,defaultView:'projects'})", app)
        self.assertIn(
            "atlasView('dashboard','Dashboard',0,'graph','dashboard')", atlas
        )
        dashboard = atlas.split("export const dashboardView={", 1)[1].split("};", 1)[0]
        self.assertIn("projectPage('dashboard')", dashboard)
        self.assertIn("section:'graph'", dashboard)
        self.assertIn("/xo/dashboard.json", atlas)
        self.assertIn("clusters:l.clusters||[]", atlas)
        self.assertIn("function drawEnclosures(k)", atlas)
        self.assertIn("if(DATA.meta.enclose)drawEnclosures(k)", atlas)
        self.assertIn("x:DATA.meta.tieSpring||", atlas)
        self.assertIn("belongsToCategory", atlas)
        self.assertIn("DATA.meta.shapeLegend", atlas)
        self.assertIn("const activeSection=v.section||v.id", registry)

    def test_dashboard_route_is_registered_before_the_static_mount(self) -> None:
        router = (ROOT / "modules" / "projects" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('"/xo/dashboard.json"', router)
        # The route serves the view out of the workspace document rather than
        # calling the builder itself; the builder is reached only through the
        # document's rebuild path (the projects module's service).
        service = (ROOT / "modules" / "projects" / "service.py").read_text(encoding="utf-8")
        self.assertIn("workspace_views.read", service)
        # the module mounts full paths (no prefix), behind its api gate
        self.assertIn('"/xo/space.json"', router)
        document = (
            ROOT / "services" / "cowork_agent" / "visualizer" / "workspace"
            / "views.py"
        ).read_text(encoding="utf-8")
        self.assertIn("build_categorized_graph", document)
        # one scan feeds both projections
        self.assertIn("build_categorized_graph(source=space)", document)


if __name__ == "__main__":
    unittest.main()
