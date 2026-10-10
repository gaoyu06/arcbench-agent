import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import compaction  # noqa: E402
import loop  # noqa: E402
from helpers import FakeLLM, done_llm, lead  # noqa: E402
from tools import Tools  # noqa: E402


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "shot.png").write_bytes(b"\x89PNG fake")
        self.tools = Tools(Path(self.tmp.name))
        loop._visual.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_vision_model_gets_image_attached(self):
        llm = type("L", (), {"vision": True})()
        text, attachment = loop.view_image(llm, self.tools, json.dumps({"path": "shot.png"}))
        self.assertIn("attached", text)
        self.assertEqual(attachment["content"][1]["type"], "image_url")
        self.assertTrue(attachment["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertLess(compaction.estimate([attachment]), compaction.IMAGE_TOKENS + 100)

    def test_text_model_gets_description_from_visual_model(self):
        llm = type("L", (), {"vision": False})()
        loop._visual["llm"] = type("V", (), {"chat": lambda self, *a: type("R", (), {"text": "a toolbar"})()})()
        text, attachment = loop.view_image(llm, self.tools, json.dumps({"path": "shot.png"}))
        self.assertIn("a toolbar", text)
        self.assertIsNone(attachment)

    def test_text_files_are_not_images(self):
        text, attachment = loop.view_image(type("L", (), {"vision": True})(), self.tools, json.dumps({"path": "a.ts"}))
        self.assertIn("not an image", text)
        self.assertIsNone(attachment)


class ParallelReadTest(unittest.TestCase):
    def test_reads_in_one_reply_run_concurrently_and_keep_order(self):
        import time as clock

        with tempfile.TemporaryDirectory() as tmp:
            tools = Tools(Path(tmp))
            for name in "abc":
                (Path(tmp) / f"{name}.txt").write_text(name)
            original = tools.run

            def slow(name, arguments):
                clock.sleep(0.3)
                return original(name, arguments)

            tools.run = slow
            calls = [{"id": n, "name": "bash", "arguments": json.dumps({"command": f"cat {n}.txt"})} for n in "abc"]
            started = clock.time()
            results = loop.prefetch_reads(
                FakeLLM(), tools, calls + [{"id": "w", "name": "apply_patch", "arguments": "{}"}]
            )
            self.assertLess(clock.time() - started, 0.8)
            self.assertEqual(list(results), ["a", "b", "c"])
            self.assertIn("a", results["a"][0])
            self.assertEqual(
                loop.prefetch_reads(
                    FakeLLM(), tools, [calls[0], {"id": "w", "name": "apply_patch", "arguments": "{}"}, calls[1]]
                ),
                {},
            )


class PatchCheckTest(unittest.TestCase):
    """apply_patch with a check argument runs the check in the same step instead of a reply of its own."""

    def run_patch(self, patch: str, check: str) -> tuple[list, str]:
        class LLM:
            vision, calls = False, 0

            def chat(self, system, messages, tools):
                self.calls += 1
                call = (
                    {"id": "p", "name": "apply_patch", "arguments": json.dumps({"input": patch, "check": check})}
                    if self.calls == 1
                    else {"id": "d", "name": "done", "arguments": json.dumps({"summary": "ok"})}
                )
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                    ],
                }
                return type("R", (), {"message": message, "tool_calls": [call], "text": ""})()

        runs, final = [], []
        tool = loop.Extra({"name": "check"}, lambda args: runs.append(args) or "CHECK PASSED\ne2e 1/1 passed")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("a\n")
            loop.run(LLM(), "sys", "task", Tools(Path(tmp)), extras={"check": tool}, final=final)
        return runs, next(m["content"] for m in final if m.get("tool_call_id") == "p")

    def test_check_runs_after_a_successful_patch(self):
        runs, result = self.run_patch("*** Begin Patch\n*** Add File: b.txt\n+b\n*** End Patch", "b.spec|c.spec")
        self.assertEqual(runs, [{"pattern": "b.spec|c.spec"}])
        self.assertTrue(result.startswith("Success"))
        self.assertIn("CHECK PASSED", result)
        runs, _ = self.run_patch("*** Begin Patch\n*** Add File: b.txt\n+b\n*** End Patch", "all")
        self.assertEqual(runs, [{"pattern": ""}])

    def test_no_check_after_a_failed_patch(self):
        runs, result = self.run_patch("*** Begin Patch\n*** Update File: a.txt\n-zzz\n+y\n*** End Patch", "b.spec")
        self.assertEqual(runs, [])
        self.assertIn("check was not run", result)


class TimeLimitTest(unittest.TestCase):
    def test_lead_is_asked_once_to_wrap_up_and_cannot_start_subagents(self):
        import time

        import roles

        with tempfile.TemporaryDirectory() as tmp:
            engineer = lead(tmp, roles.Helpers(Path(tmp), Path(tmp), "engineer", {}))
            engineer.deadline = time.time() + roles.WRAP_UP_SECONDS - 60
            self.assertIn("time limit", engineer.on_step(1, []))
            self.assertIsNone(engineer.on_step(2, []))
            self.assertIn("time limit", engineer.subagent({"task": "x", "reasoning_effort": "low", "kind": "work"}))
            engineer.helpers.jobs[1] = ("x", None)
            self.assertIn("time limit", engineer.send_subagent({"id": 1, "message": "go on"}))
            engineer.helpers.jobs.clear()
            engineer.helpers.close()

    def test_subagents_end_at_the_lead_deadline(self):
        import roles

        seen = []
        original, original_llm = roles.loop.run, roles.new_llm
        roles.loop.run = lambda *args, **kwargs: seen.append(kwargs["deadline"]) or "done"
        roles.new_llm = lambda model=None: FakeLLM()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                helpers = roles.Helpers(Path(tmp), Path(tmp), "engineer", {}, deadline=123.0)
                helpers.start("task", "low")
                helpers.result(1)
                helpers.close()
        finally:
            roles.loop.run, roles.new_llm = original, original_llm
        self.assertEqual(seen, [123.0])

    def test_session_ends_at_the_deadline(self):
        import time

        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            summary = loop.run(llm, "sys", "task", Tools(Path(tmp)), deadline=time.time() - 1)
        self.assertEqual(summary, "step or time limit reached")


class SessionEndTest(unittest.TestCase):
    def test_done_is_refused_while_subagents_run_and_close_stops_them(self):
        import threading

        import roles

        refusals, final = ["busy", None], []
        with tempfile.TemporaryDirectory() as tmp:
            summary = loop.run(
                done_llm(), "sys", "task", Tools(Path(tmp)), refuse_done=lambda: refusals.pop(0), final=final
            )
        self.assertEqual(summary, "s2")  # the refused done got the refusal as its result
        self.assertEqual(next(m["content"] for m in final if m.get("tool_call_id") == "d1"), "busy")

        release = threading.Event()
        original, original_llm = roles.loop.run, roles.new_llm

        def session(llm, system, text, tools, **kwargs):
            release.wait(5)
            kwargs["on_step"](1, [])  # the step boundary after close raises
            (tools.root / "late.txt").write_text("x")
            return "done"

        roles.loop.run, roles.new_llm = session, (lambda model=None: FakeLLM())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engineer = lead(tmp, roles.Helpers(Path(tmp), Path(tmp), "engineer", {}))
                number = engineer.helpers.start("build area 2")
                self.assertIn("still running", engineer.refuse_done())
                threading.Timer(0.2, release.set).start()
                engineer.helpers.close()  # returns once the subagent stopped
                self.assertFalse((Path(tmp) / "late.txt").exists())
                self.assertIn("session ended", engineer.helpers.result(number))
                self.assertIsNone(engineer.refuse_done())
        finally:
            roles.loop.run, roles.new_llm = original, original_llm


if __name__ == "__main__":
    unittest.main()
