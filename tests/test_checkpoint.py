import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import checks  # noqa: E402
import loop  # noqa: E402
from helpers import ScriptedLLM, lead  # noqa: E402
from tools import Tools  # noqa: E402


class CheckpointTest(unittest.TestCase):
    def test_resume_restores_files_and_transcript(self):
        from checkpoint import Transcript, commit
        from tools import shell

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell("git init -q && git config user.email t@t && git config user.name t", root, 30)
            (root / ".gitignore").write_text(".agent/\n")
            commit(root, "init")
            tools = Tools(root)
            loop.run(ScriptedLLM(), "sys", "task", tools, transcript=Transcript(root, "p"), max_steps=10)
            self.assertTrue((root / "step3.txt").exists())
            messages, _, step, _ = Transcript(root, "p").load(1)
            self.assertEqual(step, 1)
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool"])
            llm = ScriptedLLM()
            llm.calls = 1  # the resumed session continues with step 2
            loop.run(llm, "sys", "task", tools, transcript=Transcript(root, "p"), resume=1, max_steps=10)
            self.assertTrue((root / "step2.txt").exists())
            self.assertTrue(list((root / ".agent").glob("p.after-1.jsonl")))
            last = Transcript(root, "p").load()
            self.assertEqual(last[2], 4)
            self.assertEqual(len(last[0]), 9)  # task + 4 x (assistant, tool)

    def test_start_over_archives_an_earlier_run(self):
        from checkpoint import RunState, start_over

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(start_over(root))
            RunState(root).finish("build", "abc")
            (root / ".agent" / "skills").mkdir()
            (root / ".agent" / "engineer.jsonl").write_text("{}")
            (root / "PLAN.md").write_text("stage 1")
            self.assertTrue(start_over(root))
            self.assertIsNone(RunState(root).done("build"))
            self.assertFalse((root / ".agent" / "skills").exists())  # a stale copy; skills come from a tool
            self.assertFalse((root / ".agent" / "engineer.jsonl").exists())
            self.assertEqual(len(list((root / ".agent").glob("earlier-*/engineer.jsonl"))), 1)
            self.assertEqual((root / "PLAN-earlier.md").read_text(), "stage 1")

    def test_a_delivered_database_becomes_the_earlier_stage_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "backend" / "database.db").write_text("db")
            (root / "backend" / "database.db-wal").write_text("wal")
            self.assertEqual(checks.keep_stage_db(root), "")
            self.assertEqual((root / checks.STAGE_DB / "app.db").read_text(), "db")
            self.assertEqual((root / checks.STAGE_DB / "app.db-wal").read_text(), "wal")

    def test_resume_resets_files_written_after_the_checkpoint(self):
        from checkpoint import Transcript, commit
        from tools import shell

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell("git init -q && git config user.email t@t && git config user.name t", root, 30)
            (root / ".gitignore").write_text(".agent/\n")
            commit(root, "init")
            loop.run(ScriptedLLM(), "sys", "task", Tools(root), transcript=Transcript(root, "p"), max_steps=10)
            done = ScriptedLLM()
            done.calls = 3  # next call is done: nothing new is written
            loop.run(done, "sys", "task", Tools(root), transcript=Transcript(root, "p"), resume=1, max_steps=10)
            self.assertFalse((root / "step2.txt").exists())
            self.assertFalse((root / "step3.txt").exists())


class ResumeTest(unittest.TestCase):
    def test_resume_without_a_checkpoint_archives_and_a_resumed_session_gets_the_note(self):
        from checkpoint import Transcript, commit
        from tools import shell

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell("git init -q && git config user.email t@t && git config user.name t", root, 30)
            (root / ".gitignore").write_text(".agent/\n")
            commit(root, "init")
            Transcript(root, "p").message({"role": "user", "content": "old attempt"})
            loop.run(
                ScriptedLLM(),
                "sys",
                "task",
                Tools(root),
                transcript=Transcript(root, "p"),
                resume="last",
                note="resumed",
                max_steps=10,
            )
            self.assertEqual(len(list((root / ".agent").glob("p.replaced-*.jsonl"))), 1)
            messages = Transcript(root, "p").load()[0]
            self.assertEqual([m["content"] for m in messages if m["role"] == "user"], ["task"])
            llm = ScriptedLLM()
            llm.calls = 1
            loop.run(
                llm,
                "sys",
                "task",
                Tools(root),
                transcript=Transcript(root, "p"),
                resume=1,
                note="resumed",
                max_steps=10,
            )
            messages = Transcript(root, "p").load()[0]
            self.assertEqual([m["content"] for m in messages if m["role"] == "user"], ["task", "resumed"])

    def test_engineer_tells_a_resumed_lead_its_subagents_are_gone(self):
        import roles

        captured = {}
        original = roles.loop.run
        roles.loop.run = lambda *args, **kwargs: captured.update(kwargs) or "ok"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                lead(tmp, roles.Helpers(Path(tmp), Path(tmp), "engineer", {})).run("task", resume="last")
        finally:
            roles.loop.run = original
        self.assertEqual(captured["note"], roles.RESUMED)
        self.assertIn("gone", roles.RESUMED)


if __name__ == "__main__":
    unittest.main()
