import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import checks  # noqa: E402
import spec  # noqa: E402
from helpers import YAML, lead  # noqa: E402


class SpecTest(unittest.TestCase):
    def test_requirement_keeps_scenario_conditions_in_every_caller_context(self):
        sentence = "After changing a project, its public page must show the persisted value in a new browser session."
        tree = spec.Node(
            "ROOT",
            "Projects",
            "FOLDER",
            children=[
                spec.Node(
                    f"R{i}",
                    "Edit",
                    "ATOMIC",
                    "Edit a project.",
                    [{"steps": [{"keyword": "THEN", "content": sentence}]}],
                )
                for i in range(3)
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            engineer = lead(tmp)
            engineer.tree, engineer.atomics = tree, {a.id: a for a in tree.atomics}
            engineer.skills = {}
            shown = [{"role": "user", "content": spec.outline(tree)}]
            self.assertIn(sentence, engineer.requirement({"ids": ["R0"]}, shown))
            fresh = [{"role": "user", "content": "Review R0 using the requirement tool."}]
            report = engineer.requirement({"ids": ["R0"]}, fresh)
            self.assertIn(sentence, report)
            self.assertIn(
                sentence, engineer.requirement({"ids": ["R0"]}, [*fresh, {"role": "tool", "content": report}])
            )
            self.assertIn(sentence, engineer.requirement({"ids": ["R0"]}, fresh))  # after context compaction

    def test_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "requirements.yaml").write_text(YAML)
            root = spec.load(Path(tmp))
        card = spec.card(root.atomics[0])
        self.assertIn('"Rename"; "Rename item"', card)
        self.assertEqual(card.count("GIVEN seed"), 2)
        self.assertEqual(card.count("WHEN act"), 2)
        self.assertIn("Acceptance scenarios: 2", card)

    def test_card_preserves_repeated_actions_and_assertions_in_order(self):
        atomic = spec.Node(
            "R1",
            "Toggle",
            "ATOMIC",
            scenarios=[
                {
                    "steps": [
                        {"keyword": "WHEN", "content": "Enable the setting."},
                        {"keyword": "THEN", "content": "The setting is enabled."},
                        {"keyword": "WHEN", "content": "Disable the setting."},
                        {"keyword": "WHEN", "content": "Enable the setting."},
                        {"keyword": "THEN", "content": "The setting is enabled."},
                    ]
                }
            ],
        )
        card = spec.card(atomic)
        self.assertIn("WHEN Disable the setting.\n    WHEN Enable the setting.\n    THEN The setting is enabled.", card)
        self.assertEqual(card.count("THEN The setting is enabled."), 2)

    def test_scenario_conditions_keep_their_scope_and_original_step_text(self):
        condition = "A fresh browser session begins without any previously authenticated user."
        text = "The seed contains `v1.0` and `README.md`.\n" + condition
        root = spec.Node(
            "ROOT",
            "P",
            "FOLDER",
            children=[
                spec.Node(
                    f"R-{i}",
                    "A",
                    "ATOMIC",
                    "d",
                    [
                        {
                            "steps": [
                                {"keyword": "GIVEN", "content": text if i < 2 else "An existing session is active."},
                                {"keyword": "WHEN", "content": "Open the project."},
                                {"keyword": "THEN", "content": "The project page is visible."},
                            ]
                        }
                    ],
                )
                for i in range(10)
            ],
        )
        self.assertNotIn(condition, spec.outline(root))
        for atomic in root.atomics:
            card = spec.card(atomic)
            expected = "\n".join(f"    {s['keyword']} {s['content']}" for s in atomic.scenarios[0]["steps"])
            self.assertIn(expected, card)
            self.assertEqual(condition in card, atomic.id in {"R-0", "R-1"})

    def test_exact_ui_names_support_paired_smart_and_ascii_quotes(self):
        description = 'Choose “Enable feature”, then "Apply changes"; “Enable feature” stays exact.'
        self.assertEqual(spec.quoted_names(description), ["Enable feature", "Apply changes"])
        self.assertIn(
            '"Enable feature"; "Apply changes"', spec.card(spec.Node("R1", "Preferences", "ATOMIC", description))
        )
        self.assertEqual(spec.quoted_names('“mismatched" and “unfinished'), [])
        self.assertEqual(spec.quoted_names('“line\nbreak” and "line\nbreak"'), [])


class SharedRulesTest(unittest.TestCase):
    def test_group_descriptions_reach_planner_and_nodes(self):
        a = spec.Node("R-1-1", "A", "ATOMIC", "a")
        b = spec.Node("R-2-1", "B", "ATOMIC", "b")
        root = spec.Node(
            "ROOT",
            "P",
            "FOLDER",
            "product",
            children=[
                spec.Node("R-1", "G1", "FOLDER", 'The grid is named "Worksheet grid".', children=[a]),
                spec.Node("R-2", "G2", "FOLDER", "", children=[b]),
            ],
        )
        self.assertIn("Worksheet grid", spec.outline(root))
        self.assertIn("Worksheet grid", spec.shared(root))
        self.assertIn("Worksheet grid", spec.shared(root, ["R-1-1"]))
        self.assertNotIn("Worksheet grid", spec.shared(root, ["R-2-1"]))
        self.assertIn("product", spec.shared(root, ["R-2-1"]))


class SnapshotTest(unittest.TestCase):
    def test_failure_shows_the_page_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "error-context.md"
            path.write_text('# Page snapshot\n\n```yaml\n- main [ref=e2]:\n  - button "Save" [ref=e4]\n```\n')
            snapshot = checks.page_snapshot({"attachments": [{"name": "error-context", "path": str(path)}]})
            self.assertEqual(snapshot, '    - main:\n      - button "Save"')
            self.assertEqual(checks.page_snapshot({"attachments": []}), "")
            self.assertEqual(
                checks.failure_names(f"- a.spec.ts :: t\n  boom\n  Page at the failure:\n{snapshot}"),
                ["a.spec.ts :: t"],
            )


class StageChangeTest(unittest.TestCase):
    def test_group_changes_mark_descendants_and_old_snapshots_are_unverified(self):
        import hashlib

        tree = spec.Node(
            "ROOT",
            "Product",
            "FOLDER",
            children=[
                spec.Node(
                    "G",
                    "Settings",
                    "FOLDER",
                    "Keep changes for this session.",
                    children=[spec.Node("R1", "Toggle", "ATOMIC", "Toggle the setting.")],
                ),
                spec.Node("R2", "View", "ATOMIC", "Show the current value."),
            ],
        )
        before = spec.snapshot(tree)
        self.assertEqual(spec.changes(tree, before), {})
        tree.children[0].description = "Keep changes across sessions."
        self.assertEqual(spec.changes(tree, before), {"R1": "changed"})
        before_root_change = spec.snapshot(tree)
        tree.description = "All controls work with the keyboard."
        self.assertEqual(spec.changes(tree, before_root_change), {"R1": "changed", "R2": "changed"})
        legacy = {
            a.id: hashlib.sha256(
                json.dumps([a.name, a.description, a.scenarios], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            for a in tree.atomics
        }
        self.assertEqual(spec.changes(tree, legacy), {"R1": "unverified", "R2": "unverified"})

    def test_changes_mark_new_and_changed_requirements_and_all_without_a_snapshot(self):
        def tree(desc):
            return spec.Node(
                "ROOT",
                "P",
                "FOLDER",
                "",
                [],
                [spec.Node("REQ-1", "A", "ATOMIC", desc), spec.Node("REQ-2", "B", "ATOMIC", "same")],
            )

        before = spec.snapshot(tree("old"))
        after = tree("new")
        after.children.append(spec.Node("REQ-3", "C", "ATOMIC", "x"))
        self.assertEqual(spec.changes(after, before), {"REQ-1": "changed", "REQ-3": "new"})
        self.assertEqual(set(spec.changes(after, None).values()), {"unverified"})
        text = spec.outline(after, spec.changes(after, before))
        self.assertIn("REQ-1 A (0 tests) [changed]", text)
        self.assertIn("REQ-2 B (0 tests)\n", text + "\n")

    def test_earlier_snapshot_is_found_after_start_over(self):
        from checkpoint import RunState, earlier_snapshot, save_snapshot, start_over

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            RunState(root).finish("build", "abc")
            save_snapshot(root, {"REQ-1": "f"})
            self.assertTrue(start_over(root))
            self.assertEqual(earlier_snapshot(root), {"REQ-1": "f"})

    def test_missing_seeds_reads_every_text_column(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE accounts (id INTEGER, username TEXT)")
            con.execute("INSERT INTO accounts VALUES (1, 'Evo-Owner')")
            con.commit()
            con.close()
            self.assertEqual(checks.missing_seeds(db, ["evo-owner", "evo-repo"]), ["evo-repo"])

    def test_seed_scan_does_not_block_done_after_requirements_are_read(self):
        from unittest.mock import patch as mock_patch

        import roles

        helpers = roles.Helpers.__new__(roles.Helpers)
        helpers.jobs = {}
        with tempfile.TemporaryDirectory() as tmp:
            engineer = lead(tmp, helpers)
            engineer.status = {"REQ-1": "changed"}
            self.assertIn("REQ-1", engineer.refuse_done())
            engineer.read.add("REQ-1")
            report = f"build ok; fresh-database start ok; e2e 1/1 passed\n{checks.SEED_HEADER}\n- Alpha/Beta"
            with (
                mock_patch.object(checks, "check", return_value=(True, report)),
                mock_patch.object(roles, "shell", return_value=""),
            ):
                self.assertIn(checks.SEED_HEADER, engineer.check({}))
            self.assertIsNone(engineer.refuse_done())


if __name__ == "__main__":
    unittest.main()
