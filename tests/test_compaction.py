import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import compaction  # noqa: E402
from helpers import FakeLLM  # noqa: E402


class CompactionTest(unittest.TestCase):
    def setUp(self):  # the production threshold is far above what these histories reach
        self.trigger, compaction.TRIGGER_TOKENS = compaction.TRIGGER_TOKENS, 60000
        self.keep, compaction.KEEP_RECENT_TOKENS = compaction.KEEP_RECENT_TOKENS, 16000

    def tearDown(self):
        compaction.TRIGGER_TOKENS, compaction.KEEP_RECENT_TOKENS = self.trigger, self.keep

    def history(self, turns):
        messages = [{"role": "user", "content": "task"}]
        for i in range(turns):
            args = json.dumps({"path": f"f{i}.js", "content": "x" * 3000})
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": str(i), "type": "function", "function": {"name": "write", "arguments": args}}
                    ],
                }
            )
            messages.append({"role": "tool", "tool_call_id": str(i), "content": "y" * 3000})
        return messages

    def test_session_summarizes_itself_and_gets_a_fresh_task(self):
        llm, state = FakeLLM(), {}
        old = self.history(80)
        new = compaction.compact(llm, "sys", [], old, state, lambda: ("fresh task", "## Latest check report\nok"))
        sent, choice = llm.requests[0]
        self.assertEqual(sent[:-1], old)  # the whole conversation, so the cached prefix is reused
        self.assertEqual(sent[-1]["content"], compaction.SUMMARY_REQUEST)
        self.assertEqual(choice, "none")
        self.assertEqual(new[0]["content"], "fresh task")
        self.assertTrue(new[1]["content"].startswith(compaction.SUMMARY_TAG + "\nsummary 1"))
        self.assertIn("Latest check report", new[1]["content"])
        self.assertEqual(new[2]["role"], "assistant")
        self.assertEqual(new[-1], old[-1])
        self.assertLess(compaction.estimate(new), compaction.TRIGGER_TOKENS)
        again = compaction.compact(llm, "sys", [], new + self.history(80)[1:], state)
        self.assertEqual(again[0], new[0])  # without refresh the task stays
        self.assertIn("summary 1", llm.requests[1][0][1]["content"])  # the earlier summary is in the request

    def test_small_history_untouched(self):
        small = self.history(3)
        self.assertIs(compaction.compact(FakeLLM(), "sys", [], small, {}), small)


class MilestoneTest(unittest.TestCase):
    def test_milestone_compacts_a_long_history_early_and_keeps_the_recent_turns(self):
        turn = [{"role": "assistant", "content": "x" * 6000}, {"role": "user", "content": "r"}]
        turns = (compaction.MILESTONE_TOKENS + compaction.TRIGGER_TOKENS) // 2 // compaction.estimate(turn)
        messages = [{"role": "user", "content": "task"}]
        for i in range(turns):  # between the milestone threshold and the regular trigger
            messages += [{"role": "assistant", "content": "x" * 6000}, {"role": "user", "content": f"r{i}"}]
        size = compaction.estimate(messages)
        self.assertTrue(compaction.MILESTONE_TOKENS < size < compaction.TRIGGER_TOKENS)
        state = {}
        self.assertIs(compaction.compact(FakeLLM(), "s", [], messages, state), messages)  # no milestone: wait
        new = compaction.compact(FakeLLM(), "s", [], messages, state, milestone=True)
        self.assertLess(compaction.estimate(new), compaction.KEEP_RECENT_TOKENS + 10000)
        self.assertEqual(new[-1], messages[-1])


class TrimTest(unittest.TestCase):
    def history(self, turns):
        messages = [{"role": "user", "content": "task"}]
        for i in range(turns):
            path = "once.ts" if i == 0 else "app.ts"  # app.ts is rewritten every turn, once.ts never again
            args = json.dumps({"input": f"*** Begin Patch\n*** Add File: {path}\n+{'x' * 5000}\n*** End Patch"})
            messages += [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "r" * 8000,
                    "tool_calls": [
                        {"id": f"c{i}", "type": "function", "function": {"name": "apply_patch", "arguments": args}}
                    ],
                },
                {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 8000},
            ]
        return messages

    def test_short_history_is_untouched(self):
        messages = self.history(3)
        self.assertIs(compaction.trim(messages), messages)

    def test_old_turns_are_trimmed_and_recent_kept(self):
        messages = self.history(40)
        trimmed = compaction.trim(messages)
        self.assertEqual(trimmed[0], messages[0])
        self.assertEqual(trimmed[-4:], messages[-4:])
        self.assertLess(compaction.estimate(trimmed), compaction.estimate(messages) * 2 // 3)  # reasoning stays
        self.assertEqual(trimmed[1]["reasoning_content"], "r" * 8000)  # passed back verbatim
        self.assertEqual(trimmed[1], messages[1])  # once.ts was not changed again: its content stays
        rewritten = json.loads(trimmed[3]["tool_calls"][0]["function"]["arguments"])
        self.assertIn("app.ts", rewritten["input"])
        self.assertIn("trimmed", rewritten["input"])  # a later patch superseded it
        self.assertIn("trimmed", trimmed[2]["content"])
        skill = {"role": "tool", "tool_call_id": "s", "content": "---\nname: grids\n" + "x" * 2000}
        self.assertEqual(compaction._trim_message(skill), skill)  # loaded skills are kept whole
        self.assertIs(compaction.trim(trimmed), trimmed)  # stable until enough new history accumulates


if __name__ == "__main__":
    unittest.main()
