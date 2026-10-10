import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import checks  # noqa: E402
import loop  # noqa: E402
from helpers import lead  # noqa: E402


class StaticTest(unittest.TestCase):
    def test_browser_dialogs_flagged_native_select_and_router_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "frontend", "src")
            src.mkdir(parents=True)
            (src / "A.tsx").write_text(
                '<select id="a">\nconfirm("x")\nwindow.alert("y")\n<Link to="/a">\n'
                'const go = useNavigate();\n<a href="/b">\n<Routes>\n'
            )
            found = checks.static(Path(tmp))
        self.assertEqual(len(found), 2)
        self.assertTrue(all("alert/confirm/prompt" in violation for violation in found))


class LoadTest(unittest.TestCase):
    """Whole-suite failures caused by machine load must not count as regressions."""

    def test_confirm_keeps_only_failures_that_repeat(self):
        failures = "- a.spec.ts :: slow one\n  Timeout 3000ms exceeded\n- b.spec.ts :: broken\n  not visible"
        calls = []
        original = checks.e2e
        checks.e2e = lambda root, url, pattern="", config="", workers=4: (
            calls.append((pattern, workers)) or (0, 2, "- b.spec.ts :: broken\n  not visible")
        )
        try:
            remaining, recovered = checks.confirm(Path("."), "http://x", failures)
        finally:
            checks.e2e = original
        self.assertEqual(recovered, 1)
        self.assertEqual(checks.failure_names(remaining), ["b.spec.ts :: broken"])
        self.assertIn("- a.spec.ts :: slow one", remaining.split("FLAKY (failed in the parallel run", 1)[1])
        self.assertEqual(calls[0][1], 1)
        self.assertIn("--max-failures=", calls[0][0])


class CheckPatternTest(unittest.TestCase):
    def test_check_detects_behavior_broken_only_on_earlier_data(self):
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        tested = []

        def server(root, data=None):
            return SimpleNamespace(
                url="earlier" if data else "fresh", wait=lambda: "", stop=lambda: None, output=lambda: ""
            )

        def e2e(root, url, pattern="", workers=4):
            tested.append((url, pattern))
            return (
                (0, 1, "- contact.spec.ts :: retains the existing name\n  name is empty")
                if url == "earlier"
                else (1, 1, "")
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / checks.STAGE_DB).mkdir(parents=True)
            (root / "backend/test-e2e").mkdir(parents=True)
            (root / "backend/test-e2e/contact.spec.ts").touch()
            with mock_patch.multiple(checks, build=lambda root: "", Server=server, static=lambda root: [], e2e=e2e):
                ok, report = checks.check(root, "contact.spec")
                self.assertFalse(ok)
                self.assertEqual(
                    [t for t in tested if not t[1].startswith("--grep")],
                    [("fresh", "contact.spec"), ("earlier", "contact.spec")],
                )
                self.assertTrue(any(t[1].startswith("--grep") for t in tested))  # a patterned check reruns failures
                self.assertIn("earlier-stage", report)
                self.assertIn("name is empty", report)
                self.assertEqual(checks.failed_tests(report), {"contact.spec.ts :: retains the existing name"})
                with mock_patch.object(checks, "e2e", return_value=(1, 1, "")):
                    ok, report = checks.check(root, "contact.spec")
                self.assertTrue(ok, report)

    def test_failed_click_keeps_the_blocker_from_a_long_action_log(self):
        import shlex
        from unittest.mock import patch as mock_patch

        blocker = '<section role="dialog" aria-label="Inspector">Inspector</section> intercepts pointer events'
        error = (
            "TimeoutError: locator.click: Timeout 3000ms exceeded.\nCall log:\n"
            + "  - waiting for the target to be visible, enabled and stable\n" * 6
            + f"  - {blocker}\n  - {blocker}\n"
            + "  - retrying click action\n  - waiting 500ms\n" * 14
        )
        report = {
            "suites": [
                {
                    "specs": [
                        {
                            "file": "panel.spec.ts",
                            "title": "edit beside a panel",
                            "tests": [
                                {
                                    "status": "unexpected",
                                    "results": [{"status": "failed", "errors": [{"message": error}]}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend/test-e2e").mkdir(parents=True)
            (root / "backend/test-e2e/panel.spec.ts").write_text("")

            def shell(command, *args, **kwargs):
                words = shlex.split(command)
                Path(words[words.index(">") + 1]).write_text(json.dumps(report))
                return "[exit 1]"

            with mock_patch.object(checks, "shell", shell):
                passed, total, failures = checks.e2e(root, "http://example.invalid")
        self.assertEqual((passed, total), (0, 1))
        self.assertEqual(failures.count(blocker), 1)
        self.assertIn("TimeoutError: locator.click", failures)
        self.assertEqual(checks.failure_names(failures), ["panel.spec.ts :: edit beside a panel"])

    def test_failed_retry_keeps_the_first_error_and_its_page(self):
        import shlex
        from unittest.mock import patch as mock_patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend/test-e2e").mkdir(parents=True)
            (root / "backend/test-e2e/save.spec.ts").write_text("")
            results = []
            for attempt, (error, heading) in enumerate(
                [
                    ("success message missing after write", "Saved record"),
                    ("initial value already changed on retry", "Changed record"),
                ]
            ):
                snapshot = root / f"attempt-{attempt}.md"
                snapshot.write_text(f'# Page snapshot\n- heading "{heading}"\n')
                results.append(
                    {
                        "status": "failed",
                        "retry": attempt,
                        "errors": [{"message": error}],
                        "attachments": [{"name": "error-context", "path": str(snapshot)}],
                    }
                )
            report = {
                "suites": [
                    {
                        "specs": [
                            {
                                "file": "save.spec.ts",
                                "title": "saves a record",
                                "tests": [{"status": "unexpected", "results": results}],
                            }
                        ]
                    }
                ]
            }

            def shell(command, *args, **kwargs):
                words = shlex.split(command)
                Path(words[words.index(">") + 1]).write_text(json.dumps(report))
                return "[exit 1]"

            with mock_patch.object(checks, "shell", shell):
                passed, total, failures = checks.e2e(root, "http://example.invalid")
            self.assertEqual((passed, total), (0, 1))
            self.assertIn("success message missing after write", failures)
            self.assertIn('heading "Saved record"', failures)
            self.assertNotIn('heading "Changed record"', failures)

    def test_failed_check_includes_backend_runtime_output(self):
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        backend = "Listening on port 3000\n[api error] ReferenceError: loadRecord is not defined\n"
        server = SimpleNamespace(
            url="http://example.invalid", wait=lambda: "", stop=lambda: None, output=lambda: backend
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend/test-e2e").mkdir(parents=True)
            (root / "backend/test-e2e/load.spec.ts").write_text("")
            with mock_patch.multiple(
                checks,
                build=lambda root: "",
                Server=lambda root: server,
                static=lambda root: [],
                e2e=lambda *args, **kwargs: (0, 1, "- load.spec.ts :: opens item\n  heading not found"),
            ):
                ok, report = checks.check(root, "load.spec")
                self.assertFalse(ok)
                self.assertIn("ReferenceError: loadRecord is not defined", report)
                with mock_patch.object(checks, "e2e", return_value=(1, 1, "")):
                    ok, report = checks.check(root, "load.spec")
                self.assertTrue(ok)
                self.assertNotIn(backend.strip(), report)

    def test_several_spec_files_reach_playwright_as_quoted_filters(self):
        self.assertEqual(checks.spec_filter("sort.spec|filter.spec"), "sort.spec filter.spec")
        self.assertEqual(checks.spec_filter("TEMP DIAGNOSTIC"), "TEMP DIAGNOSTIC")
        self.assertEqual(checks.spec_filter("a;b"), "'a;b'")
        self.assertEqual(checks.spec_filter(""), "")
        seen = []

        class Server:
            url = "http://x"

            def __init__(self, root):
                pass

            def wait(self):
                return ""

            def output(self):
                return ""

            def stop(self):
                pass

        saved = checks.build, checks.Server, checks.e2e, checks.static
        checks.build, checks.Server, checks.static = (lambda root: ""), Server, (lambda root: [])
        checks.e2e = lambda root, url, pattern="", config="", workers=4: seen.append(pattern) or (2, 2, "")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "backend" / "test-e2e").mkdir(parents=True)
                for name in ("sort", "filter"):
                    (Path(tmp) / "backend" / "test-e2e" / f"{name}.spec.ts").write_text("")
                ok, report = checks.check(Path(tmp), "sort.spec|filter.spec")
        finally:
            checks.build, checks.Server, checks.e2e, checks.static = saved
        self.assertTrue(ok)
        self.assertEqual(seen, ["sort.spec filter.spec"])


class UnmatchedPatternTest(unittest.TestCase):
    def test_off_values_and_unmatched_patterns_skip_the_run(self):
        self.assertIsNone(loop.requested_check(json.dumps({"input": "", "check": "false"})))
        self.assertEqual(loop.requested_check(json.dumps({"input": "", "check": "all"})), "")
        with tempfile.TemporaryDirectory() as tmp:
            specs = Path(tmp) / "backend" / "test-e2e"
            specs.mkdir(parents=True)
            (specs / "sort.spec.ts").write_text("")
            self.assertEqual(checks.unmatched(Path(tmp), "sort|filter"), "")
            self.assertIn("sort.spec.ts", checks.unmatched(Path(tmp), "pivot"))
            ok, report = checks.check(Path(tmp), "pivot")
            self.assertFalse(ok)
            self.assertIn("no e2e spec file matches", report)


class ReportTest(unittest.TestCase):
    def test_failed_tests_excludes_flaky(self):
        report = (
            "build ok; fresh-database start ok; e2e 1/2 passed\nFAILED TESTS:\n- a.spec.ts :: breaks\n"
            "E2E FAILURES:\n- a.spec.ts :: breaks\n  boom"
        )
        self.assertEqual(checks.failed_tests(report), {"a.spec.ts :: breaks"})


class CheckToolTest(unittest.TestCase):
    def test_all_runs_the_whole_suite(self):
        seen, original = [], checks.check
        checks.check = lambda root, pattern="", seeds=None: seen.append(pattern) or (True, "e2e 2/2 passed")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engineer = lead(tmp)
                self.assertTrue(engineer.check({"pattern": " all "}).startswith("CHECK PASSED"))
                self.assertTrue(engineer.settled)
                engineer.check({"pattern": "ALL"}, lead=False)
        finally:
            checks.check = original
        self.assertEqual(seen, ["", ""])

    def test_app_server_gets_no_secrets(self):
        import os
        from unittest import mock

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"ARCBENCH_COOKIE": "c", "ARCBENCH_PASSWORD": "p", "OPENAI_API_KEY": "k"}),
            mock.patch.object(checks.subprocess, "Popen") as popen,
        ):
            checks.Server(Path(tmp))
        env = popen.call_args.kwargs["env"]
        for name in ("ARCBENCH_COOKIE", "ARCBENCH_PASSWORD", "OPENAI_API_KEY"):
            self.assertNotIn(name, env)
        self.assertIn("PORT", env)


if __name__ == "__main__":
    unittest.main()
