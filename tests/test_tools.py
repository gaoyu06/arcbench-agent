import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helpers import patch  # noqa: E402
from tools import Tools  # noqa: E402


class ToolsTest(unittest.TestCase):
    def test_partial_patch_reports_sections_when_a_path_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.js"
            source.write_text("original\n")
            result = patch(
                Tools(root),
                "*** Update File: app.js\n-original\n+updated\n"
                "*** Update File: app.js\n-missing\n+replacement\n"
                "*** Add File: other.js\n+other",
            )
            self.assertEqual(source.read_text(), "updated\n")
            self.assertEqual((root / "other.js").read_text(), "other\n")
            self.assertIn("M app.js", result)
            self.assertIn("- app.js:", result)
            self.assertIn("Failed sections were not applied", result)
            self.assertNotIn("files were left unchanged", result)

    def test_text_after_patch_end_is_rejected_without_changing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.js"
            source.write_text("original\n")
            tools = Tools(root)
            result = tools.run(
                "apply_patch",
                json.dumps(
                    {
                        "input": "*** Begin Patch\n*** Add File: app.js\n+export const value = 1;\n*** End Patch\n"
                        '<metadata name="check">smoke</metadata>'
                    }
                ),
            )
            self.assertTrue(result.startswith("ERROR:"), result)
            self.assertEqual(source.read_text(), "original\n")

    def test_patch_refuses_ambiguous_changes_and_paths_stay_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Tools(Path(tmp))
            self.assertIn("A a/b.txt", patch(tools, "*** Add File: a/b.txt\n+x\n+x"))
            self.assertIn("match 2 places", patch(tools, "*** Update File: a/b.txt\n-x\n+y"))
            self.assertIn("outside", patch(tools, "*** Add File: ../escape.txt\n+x"))
            self.assertIn("node_modules", patch(tools, "*** Add File: node_modules/x.js\n+x"))
            self.assertIn("A c.txt", tools.run("apply_patch", json.dumps({"input": "*** Add File: c.txt\n+x"})))
            self.assertIn("A e.txt", patch(tools, "*** Add File: d.txt\n+x\n*** End Patch\n*** Add File: e.txt\n+y"))
            self.assertEqual((Path(tmp) / "d.txt").read_text(), "x\n")
            patch(tools, "*** Add File: q.js\n+db.all(`SELECT 1\n   FROM t`);")
            self.assertEqual((Path(tmp) / "q.js").read_text(), "db.all(`SELECT 1\n   FROM t`);\n")

    def test_bash_refuses_e2e_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            lead = Tools(Path(tmp), experiments=False)
            for command in (
                "cd backend && npx playwright test a.spec.ts",
                "npm run test:e2e -- a.spec.ts",
                "node src/index.js & sleep 2; curl localhost:3000",
                "cd backend && npm start",
            ):
                self.assertIn("subagent", lead.run("bash", json.dumps({"command": command})), command)
            self.assertIn("exit 0", lead.run("bash", json.dumps({"command": "grep -rn start . || true"})))
            # read-only searches for such commands are not experiments
            self.assertIn("[exit", lead.run("bash", json.dumps({"command": 'rg -n "npm start" . || true'})))
            self.assertNotIn("subagent", lead.run("bash", json.dumps({"command": 'grep -rn "playwright test" .'})))
            self.assertNotIn("subagent", Tools(Path(tmp)).run("bash", json.dumps({"command": "echo npm start"})))

    def test_bash_kills_background_children(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            out = Tools(Path(tmp)).run(
                "bash",
                json.dumps({"command": "(sleep 0.4; echo leaked > child-finished) & echo started", "timeout": 2}),
            )
            self.assertIn("started", out)
            self.assertIn("[exit 0]", out)
            time.sleep(0.5)
            self.assertFalse((Path(tmp) / "child-finished").exists())

    def test_bash_times_out_foreground_and_keeps_its_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Tools(Path(tmp)).run("bash", json.dumps({"command": "echo before-wait; sleep 20", "timeout": 1}))
            self.assertIn("before-wait", out)
            self.assertIn("killed after 1s timeout", out)


class HarnessSpeedTest(unittest.TestCase):
    def test_patch_writes_good_files_and_reports_failed_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, root = Tools(Path(tmp)), Path(tmp)
            (root / "a.js").write_text("one\ntwo\n")
            bad = patch(tools, "*** Add File: new.js\n+x\n*** Update File: a.js\n-missing\n+y")
            self.assertIn("1 of 2 file sections failed", bad)
            self.assertIn("A new.js", bad)
            self.assertIn("change 1", bad)
            self.assertEqual((root / "new.js").read_text(), "x\n")
            half = patch(tools, "*** Update File: a.js\n-one\n+1\n@@\n-nope\n+y")
            self.assertIn("Failed sections were not applied", half)
            self.assertEqual((root / "a.js").read_text(), "one\ntwo\n")  # never half edited
            ok = patch(tools, "*** Update File: a.js\n-one\n+1\n@@\n-two\n+2\n*** Delete File: new.js")
            self.assertIn("Success", ok)
            self.assertEqual((root / "a.js").read_text(), "1\n2\n")
            self.assertFalse((root / "new.js").exists())

    def test_patch_locates_changes_like_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, root = Tools(Path(tmp)), Path(tmp)
            (root / "a.js").write_text("function a() {\n  return 1;\n}\nfunction b() {\n  return 1;\n}\n")
            # an @@ line picks the second of two equal lines; indentation differences are tolerated
            self.assertIn("Success", patch(tools, "*** Update File: a.js\n@@ function b() {\n-return 1;\n+  return 2;"))
            self.assertEqual((root / "a.js").read_text().count("return 2"), 1)
            self.assertIn("return 2;\n}\n", (root / "a.js").read_text())
            # a context line without its leading space is still context
            self.assertIn(
                "Success", patch(tools, "*** Update File: a.js\n function a() {\n-  return 1;\n+  return 0;\n}")
            )
            self.assertTrue((root / "a.js").read_text().startswith("function a() {\n  return 0;\n}\n"))
            # the @@ line repeated as the first context line, as in a unified diff
            self.assertIn("Success", patch(tools, "*** Update File: a.js\n@@ function b() {\n+// b\n function b() {"))
            self.assertIn("}\n// b\nfunction b() {", (root / "a.js").read_text())
            # + lines ended by *** End of File are appended; Move to renames
            self.assertIn("Add File to replace", patch(tools, "*** Update File: a.js\n+// end"))
            self.assertIn("M b.js", patch(tools, "*** Update File: a.js\n*** Move to: b.js\n+// end\n*** End of File"))
            self.assertTrue((root / "b.js").read_text().endswith("}\n// end\n"))
            self.assertFalse((root / "a.js").exists())
            near = patch(tools, "*** Update File: b.js\n function b() {\n-  return 3;\n+  return 4;")
            self.assertIn("first 1 lines match file lines 5-5", near)
            self.assertIn("'  return 2;' (file line 6)", near)
            self.assertIn("Closest lines", patch(tools, "*** Update File: b.js\n-  return 3;\n+  return 4;"))

    def test_read_only_commands(self):
        from tools import is_read_only

        for command in (
            "grep -rn x src | head",
            "git diff --stat",
            "cd backend && cat package.json",
            "sed -n '10,40p' src/app.js",
            "nl -ba a.ts | sed -n '5,9p'",
            "sed -n '/\"test:e2e\"/,/}/p' package.json",
            "sed -n '100,$p' f",
        ):
            self.assertTrue(is_read_only(command), command)
        for command in (
            "npm test",
            "sed -i s/a/b/ f",
            "echo x > f",
            "git checkout .",
            "find . -delete",
            "sed -n 'w out' f",
            "sed '1e date' f",
            "awk '{print}' f",
            "tree -o out",
            "find . -fls out",
        ):
            self.assertFalse(is_read_only(command), command)

    def test_usage_report_groups_by_role(self):
        import llm

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            rows = [
                {
                    "model": "m",
                    "label": "engineer",
                    "prompt": 100,
                    "cached": 50,
                    "completion": 10,
                    "reasoning": 5,
                    "seconds": 1.0,
                },
                {
                    "model": "m",
                    "label": "engineer:helper1",
                    "prompt": 10,
                    "cached": 0,
                    "completion": 1,
                    "reasoning": 0,
                    "seconds": 0.5,
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows))
            report = llm.usage_report(path)
            self.assertIn("role=engineer: calls=1 prompt=100 cached=50 (50%)", report)
            self.assertIn("role=helper", report)

    def test_code_map_lists_declarations(self):
        import roles

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend/src").mkdir(parents=True)
            (root / "backend/src/routes.js").write_text(
                "const x = 1;\nrouter.get('/api/items', h);\nfunction load(id) {}\nmodule.exports = { load };\n"
            )
            text = roles.code_map(root)
            self.assertIn("backend/src/routes.js (5 lines)", text)
            self.assertIn("router.get('/api/items'", text)
            self.assertIn("function load(id)", text)
            self.assertNotIn("const x", text)


if __name__ == "__main__":
    unittest.main()
