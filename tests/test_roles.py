import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import spec  # noqa: E402
from helpers import FakeLLM, lead  # noqa: E402


class PromptTest(unittest.TestCase):
    def test_system_prompt_renders(self):
        import roles

        self.assertIn("exact: true", roles.ENGINEER)
        self.assertIn("small verified slices", roles.ENGINEER)

    def test_task_does_not_report_no_seed_for_an_unmatched_precondition(self):
        import main

        precondition = "A project called `Atlas` already exists."
        tree = spec.Node(
            "ROOT",
            "Projects",
            "FOLDER",
            children=[
                spec.Node(
                    "R1",
                    "Rename",
                    "ATOMIC",
                    "Rename a project.",
                    [{"steps": [{"keyword": "GIVEN", "content": precondition}]}],
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            task = main.Builder(Path(tmp), Path(tmp), tree, 10).task()
        self.assertNotIn("Seed data stated in scenario preconditions\n- none", task)
        self.assertIn(precondition, spec.card(tree.atomics[0]))


class HelperTest(unittest.TestCase):
    def test_results_arrive_as_messages_without_waiting(self):
        import threading

        import roles

        release = threading.Event()
        original = roles.loop.run

        def slow(*args, **kwargs):
            release.wait(5)
            return "found it"

        roles.loop.run = slow
        original_llm = roles.new_llm
        roles.new_llm = lambda model=None: FakeLLM()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                helpers = roles.Helpers(Path(tmp), Path(tmp), "engineer", {})
                number = helpers.start("look into it")
                self.assertEqual(helpers.finished(), [])  # still running: nothing delivered, nothing blocked
                release.set()
                self.assertIn("found it", helpers.result(number))
                self.assertEqual(helpers.finished(), [])  # delivered once
                helpers.close()
        finally:
            roles.loop.run, roles.new_llm = original, original_llm

    def test_messages_reach_running_and_finished_subagents(self):
        import threading

        import roles

        release, seen = threading.Event(), []
        original, original_llm = roles.loop.run, roles.new_llm

        def session(llm, system, text, tools, **kwargs):
            seen.append(text)
            if len(seen) == 1:
                release.wait(5)
                seen.append(kwargs["on_step"](1, []))  # a step boundary: the queued message is taken
            kwargs["final"][:] = [{"role": "user", "content": text}]
            return f"answer {len(seen)}"

        roles.loop.run, roles.new_llm = session, (lambda model=None: FakeLLM())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                helpers = roles.Helpers(Path(tmp), Path(tmp), "engineer", {})
                number = helpers.start("review area 1")
                self.assertIn("queued", helpers.send(number, "also check the dialog"))
                release.set()
                self.assertIn("answer 2", helpers.result(number))
                self.assertIn("also check the dialog", seen[1])
                self.assertIn("continues", helpers.send(number, "now fix what you found"))
                self.assertIn("answer 3", helpers.result(number))
                self.assertIn("now fix what you found", seen[2])
                helpers.close()
        finally:
            roles.loop.run, roles.new_llm = original, original_llm

    def test_subagents_are_asked_to_wrap_up_at_their_budget(self):
        import roles

        original, original_llm = roles.loop.run, roles.new_llm
        steps = {}
        efforts = []

        def session(llm, system, text, tools, **kwargs):
            efforts.append(llm.effort)
            steps[text] = {n: kwargs["on_step"](n, []) for n in (1, roles.REVIEW_STEPS, roles.HELPER_STEPS)}
            return "done"

        roles.loop.run, roles.new_llm = session, (lambda model=None: FakeLLM())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                helpers = roles.Helpers(Path(tmp), Path(tmp), "engineer", {})
                tool = lead(tmp, helpers).extras(lead=True)["subagent"]
                tool.run({"task": "Implement review controls", "reasoning_effort": "high", "kind": "work"})
                helpers.result(1)
                tool.run(
                    {
                        "task": "Verify existing behavior against requirements",
                        "reasoning_effort": "high",
                        "kind": "review",
                    }
                )
                helpers.result(2)
                helpers.close()
            build, review = (
                next(v for k, v in steps.items() if task in k)
                for task in ("Implement review controls", "Verify existing behavior against requirements")
            )
            self.assertIsNone(build[1])
            self.assertIsNone(build[roles.REVIEW_STEPS])
            self.assertEqual(build[roles.HELPER_STEPS], roles.WRAP_UP.format(steps=roles.HELPER_STEPS))
            self.assertEqual(review[roles.REVIEW_STEPS], roles.WRAP_UP.format(steps=roles.REVIEW_STEPS))
            self.assertIsNone(review[roles.HELPER_STEPS])
            self.assertEqual(efforts, ["high", "max"])
        finally:
            roles.loop.run, roles.new_llm = original, original_llm


class SkillTest(unittest.TestCase):
    def test_skill_tool_lists_skills_and_the_requirement_tool_attaches_matching_ones_once(self):
        import compaction
        import roles

        saved = roles.SKILLS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                roles.SKILLS = Path(tmp) / "skills"
                for name, triggers in (("grids", "\\bcells?\\b"), ("clipboard", "paste")):
                    (roles.SKILLS / name).mkdir(parents=True)
                    (roles.SKILLS / name / "SKILL.md").write_text(
                        f"---\nname: {name}\ndescription: About {name}.\ntriggers: {triggers}\n---\n- {name} rule\n"
                    )
                engineer = lead(tmp)
                engineer.skills = roles.skills()
                schema = roles.skill_schema(engineer.skills)
                self.assertIn("- grids: About grids.", schema["description"])
                self.assertEqual(schema["parameters"]["properties"]["names"]["items"]["enum"], ["clipboard", "grids"])
                self.assertIn("- grids rule", engineer.skill({"names": ["grids"]}))
                self.assertIn("ERROR", engineer.skill({"names": ["nope"]}))

                atomic = spec.Node("REQ-1", "Edit", "ATOMIC", "The user edits a cell.")
                engineer.tree, engineer.atomics = atomic, {"REQ-1": atomic}
                first = engineer.requirement({"ids": ["REQ-1"]}, [])
                self.assertIn("- grids rule", first)
                self.assertNotIn("clipboard rule", first)
                again = engineer.requirement({"ids": ["REQ-1"]}, [{"role": "tool", "content": first}])
                self.assertNotIn("- grids rule", again)  # already in the conversation

                trimmed = compaction._trim_message({"role": "tool", "tool_call_id": "r", "content": "x" * 2000 + first})
                self.assertIn("- grids rule", trimmed["content"])  # attached skills survive trimming
                self.assertIn("trimmed", trimmed["content"])
        finally:
            roles.SKILLS = saved


class IndependentHelperTest(unittest.TestCase):
    def test_new_task_starts_without_lead_history_at_the_requested_effort(self):
        from unittest.mock import patch

        import roles

        seen = []

        def session(llm, system, text, tools, **kwargs):
            seen.append((kwargs.get("history"), llm.effort, text))
            return "read complete"

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(roles, "new_llm", return_value=FakeLLM()),
            patch.object(roles.loop, "run", side_effect=session),
        ):
            engineer = lead(tmp, roles.Helpers(Path(tmp), Path(tmp), "engineer", {}))
            try:
                tool = engineer.extras(lead=True)["subagent"]
                self.assertNotIn("fork_context", tool.schema["parameters"]["properties"])
                self.assertIn("reasoning_effort", tool.schema["parameters"]["required"])
                self.assertFalse(tool.context)
                tool.run({"task": "summarize the API routes", "reasoning_effort": "low", "kind": "work"})
                engineer.helpers.result(1)
                self.assertIsNone(seen[0][0])
                self.assertEqual(seen[0][1], "low")
                self.assertIn("summarize the API routes", seen[0][2])
            finally:
                engineer.helpers.close()

    def test_subagent_sees_the_same_tools_but_cannot_nest(self):
        import roles

        with tempfile.TemporaryDirectory():
            tree = spec.Node("ROOT", "Demo", "FOLDER", "", [], [])
            engineer = roles.Engineer.__new__(roles.Engineer)
            engineer.atomics, engineer.tree, engineer.skills = {}, tree, roles.skills()
            lead, child = engineer.extras(lead=True), engineer.extras(lead=False)
            self.assertEqual([e.schema for e in lead.values()], [e.schema for e in child.values()])
            self.assertIn("only the lead", child["subagent"].run({}, [], "x"))


class SubagentTaskTest(unittest.TestCase):
    def test_compaction_restores_the_subagent_task_and_listings_show_the_latest_message(self):
        import threading

        import roles

        seen, release = [], threading.Event()
        original, original_llm = roles.loop.run, roles.new_llm

        def session(llm, system, text, tools, **kwargs):
            seen.append((text, kwargs["refresh"]()))
            release.wait(5)
            return "ok"

        roles.loop.run, roles.new_llm = session, (lambda model=None: FakeLLM())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engineer = lead(tmp, roles.Helpers(Path(tmp), Path(tmp), "engineer", {}))
                number = engineer.helpers.start("review area 1")
                engineer.helpers.send(number, "also check the dialog")
                self.assertIn("also check the dialog", engineer.refresh()[1])
                release.set()
                engineer.helpers.result(number)
                engineer.helpers.close()
            text, (task, facts) = seen[0]
            self.assertTrue(text.startswith("## Your role"))
            self.assertTrue(task.startswith("## Your role"))
            self.assertIn("review area 1", task)
            self.assertNotIn("conversation so far", task)
        finally:
            roles.loop.run, roles.new_llm = original, original_llm


if __name__ == "__main__":
    unittest.main()
