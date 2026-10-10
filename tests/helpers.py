"""Fakes and fixtures shared by the tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import Tools  # noqa: E402

YAML = """
id: ROOT
name: Demo
type: FOLDER
description: Demo product.
children:
- id: REQ-1
  name: Area
  type: FOLDER
  children:
  - id: REQ-1-1
    name: Rename
    type: ATOMIC
    description: The "Rename" button opens a dialog named "Rename item".
    scenarios:
    - steps: [{keyword: GIVEN, content: seed}, {keyword: WHEN, content: act}]
    - steps: [{keyword: GIVEN, content: seed}, {keyword: WHEN, content: act}]
"""


def patch(tools: Tools, body: str) -> str:
    return tools.run("apply_patch", json.dumps({"input": f"*** Begin Patch\n{body}\n*** End Patch"}))


class FakeLLM:
    def __init__(self):
        self.requests = []

    def chat(self, system, messages, tools, tool_choice=None):
        self.requests.append((messages, tool_choice))
        return type("R", (), {"text": f"summary {len(self.requests)}"})()


class ScriptedLLM:
    """Writes step<n>.txt on each call, then calls done on the fourth call."""

    vision = False

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools):
        self.calls += 1
        n = self.calls
        if n >= 4:
            call = {"id": f"c{n}", "name": "done", "arguments": json.dumps({"summary": "ok"})}
        else:
            call = {
                "id": f"c{n}",
                "name": "apply_patch",
                "arguments": json.dumps({"input": f"*** Begin Patch\n*** Add File: step{n}.txt\n+{n}\n*** End Patch"}),
            }
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


def done_llm():
    """Calls done on every reply, with summary s<n>."""

    class LLM:
        vision, calls = False, 0

        def chat(self, system, messages, tools):
            self.calls += 1
            call = {"id": f"d{self.calls}", "name": "done", "arguments": json.dumps({"summary": f"s{self.calls}"})}
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {"name": "done", "arguments": call["arguments"]}}
                ],
            }
            return type("R", (), {"message": message, "tool_calls": [call], "text": ""})()

    return LLM()


def lead(tmp: str, helpers=None):
    """An Engineer without a model or requirements."""
    import roles

    engineer = roles.Engineer.__new__(roles.Engineer)
    engineer.root, engineer.req_dir, engineer.label, engineer.steps = Path(tmp), Path(tmp), "engineer", 10
    engineer.llm, engineer.task_builder, engineer.last_report, engineer.built = FakeLLM(), None, "", []
    engineer.publish, engineer.deadline, engineer.warned = None, None, False
    engineer.settled, engineer.helpers, engineer.skills = False, helpers, {}
    engineer.status, engineer.read, engineer.seeds = {}, set(), []
    return engineer
