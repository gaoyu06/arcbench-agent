"""Agent roles: the engineer who builds the app and the helpers it starts in the background."""

from __future__ import annotations

import concurrent.futures as futures
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import checkpoint
import checks
import loop
import spec
from llm import EFFORTS, LLM
from tools import Tools, shell

HERE = Path(__file__).resolve().parent
RULES = (HERE / "rules.md").read_text()
SOP = (HERE / "sop.md").read_text()
PLAN_FILE = "PLAN.md"
_print_lock = threading.Lock()

ENGINEER = f"""You are a senior full-stack engineer working in a git workspace with a React (Vite) frontend in \
frontend/ and an Express + SQLite backend in backend/ (dependencies installed). Paths are relative to the \
workspace root. The check tool builds the frontend, starts the backend on a fresh database and runs the \
Playwright tests in backend/test-e2e; the lead runs tests only through check, while a subagent may start servers \
and browsers for experiments.

## Tool calls
Every reply re-sends the whole conversation, so the number of replies sets the cost and the time of the build. \
Put independent tool calls in one reply: read all the files the next decision needs at once, and write a whole \
slice in one apply_patch with its check argument set to the area's spec pattern.

## Subagents
A subagent starts with only its task and these instructions, works in the same workspace and reports back as a \
message, so you keep working while it runs. Delegate bounded work that is off your critical path:
- reviewing a finished area against its requirement ids and files, once, when its tests pass;
- drafting the tests of the next area from its requirement text;
- building an area in files nobody else is editing;
- investigating a failure whose cause the report does not show, instead of experimenting yourself;
- reading many files, long requirement text or reference images and reporting what you need.
Do the blocking step, the area you are building and the fixes you already know yourself. Give each subagent a \
self-contained task naming its requirement ids, the files it may change and the output you need; keep the file \
sets of parallel work disjoint. Choose reasoning_effort by difficulty: low for finding and summarizing, high for \
building and testing, max for a hard diagnosis. Review a subagent's changes before relying on them. Fix each \
review finding that quotes the requirements, or reject it by quoting the sentence it contradicts. Use \
send_subagent to continue the same task; start a new subagent for different work. Do not wait for a subagent \
that builds or investigates: its result arrives as a message by itself, so keep working and never poll with sleep.
When every area is done, its review findings are handled and check passes without a pattern, call done in that \
reply; add nothing after it.

## Skills
Skills describe how to build kinds of components and concerns (forms and dialogs, menus and selects, grids, \
clipboard, persistence, web UI, your own e2e tests). The requirement tool attaches the skills its requirements \
need; call skill for any other before writing or reviewing code of that kind, and name the skills a subagent \
needs in its task.

{RULES}

{SOP}
"""

BUILD_TASK = """Build the product below as one working app in this workspace, end to end.
Workspace root: {root}. Every bash call starts here; use relative paths. All work stays within this workspace \
and the supplied requirement directory; read requirements through the requirement tool, not by parsing the files.

1. Plan from the outline below: write {plan} briefly at the level of architecture (tables, seed data, API \
conventions, pages and URLs, shared components) and an ordered list of feature areas, each with its atomic \
requirement ids, so that each area builds on finished ones.
2. Build the shared base (schema, seeding, page shells, shared components, backend/test-e2e/helpers.ts), then \
the areas in order as the working procedure describes, reading each area's requirements in one call when you \
start it.
3. Record the design of each area (structure, names, rules, edge cases) under it in {plan}: only files outlast \
the conversation.
4. When every area is done, run check without a pattern, fix what fails, then call done.

Reference images named in the requirements are under {req_dir}; look at them with view_image.
{earlier}
## Workspace files (read the ones you need)
{template}

{outline}"""

EARLIER_WORK = """
## Earlier work in this workspace
The workspace holds an app built for earlier requirements (their plan is PLAN-earlier.md, their tests are in \
backend/test-e2e). The requirements below are the contract now and may extend or change the earlier ones. The \
outline marks each requirement [new], [changed] (its text differs from what the earlier stage built) or \
[unverified] (the earlier text is unknown); unmarked ones are unchanged. Treat every marked requirement as not \
yet satisfied: compare its current text with the app and build each difference as an area, first making a test \
fail on the difference, then changing the app. Extend the existing data model, seed data, pages, components and \
helpers instead of rewriting them. {plan} covers this stage's work.
Before changing anything, run check without a pattern once and note which earlier tests already fail. Every \
earlier test that passed must still pass at the end, unless a changed requirement replaces the behaviour it \
asserts; then update it. The evaluator starts this stage's app on the database the earlier stage left, so schema \
changes add tables or columns with defaults and keep existing rows valid; check runs the tests on a copy of that \
data as well as on a fresh database. Keep existing rows and user changes when fixing a migration or seeding; a \
test's setup data is no reason to reset saved data.
"""

REFRESH_TASK = """Continue building the product below as one working app in this workspace; your earlier \
progress is summarised in the next message. Follow {plan}, read requirement text with the requirement tool, \
delegate side work to subagents, and when every area is done run check without a pattern, fix what fails, \
then call done.
Workspace root: {root}. Every bash call starts here; use relative paths.
{earlier}
## {plan}
{plan_text}

## Code map (every source file with its declarations; read a file only when you need its body)
{outline_code}

{outline}"""


SKILLS = HERE / "skills"  # <name>/SKILL.md; frontmatter: name, description, triggers (a regex over requirement text)


@dataclass
class Skill:
    description: str
    triggers: re.Pattern | None  # requirement text that makes the requirement tool attach the skill
    text: str  # the whole file; it starts with "---\nname: ", which compaction keeps whole


def skills() -> dict[str, Skill]:
    """Every skill by name."""
    found = {}
    for path in sorted(SKILLS.glob("*/SKILL.md")) if SKILLS.is_dir() else []:
        text = path.read_text()
        match = re.match(r"---\n(.*?)\n---\n", text, re.S)
        if not match:
            continue
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", match[1], re.M))
        triggers = meta.get("triggers", "").strip()
        found[meta.get("name", path.parent.name).strip()] = Skill(
            meta.get("description", "").strip(), re.compile(triggers, re.I) if triggers else None, text.strip()
        )
    return found


def skill_schema(found: dict[str, Skill]) -> dict:
    listed = "\n".join(f"- {name}: {s.description}" for name, s in found.items())
    return {
        "name": "skill",
        "description": "Read skills: the markup and behaviour rules for a kind of component "
        "or concern. Call it before writing, changing or reviewing code a skill covers, with every skill you "
        "need in one call; a skill already in the conversation needs no second call. Where a skill and the "
        f"requirement text differ, the requirement wins.\n{listed}",
        "parameters": {
            "type": "object",
            "properties": {"names": {"type": "array", "items": {"type": "string", "enum": list(found)}}},
            "required": ["names"],
        },
    }


def has_skill(messages: list[dict], name: str) -> bool:
    """Whether the conversation already holds the skill (it survives trimming; a compaction summary drops it)."""
    mark = f"---\nname: {name}\n"
    return any(m.get("role") == "tool" and mark in str(m.get("content") or "") for m in messages)


def log(label: str, text: str) -> None:
    with _print_lock:
        print(f"[{label}] {text}", flush=True)


SOURCE_DIRS = ("backend/src", "frontend/src", "backend/test-e2e")
SOURCE_TYPES = {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".sql", ".json"}
SYMBOL = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum)\s+[\w$]+"
    r"|(?:async\s+)?function\s+[\w$]+\s*\([^)]*\)"
    r"|module\.exports\s*=.*|exports\.[\w$]+\s*="
    r"|(?:router|app)\.(?:get|post|put|patch|delete|use)\(\s*['\"`][^'\"`]*['\"`]"
    r"|CREATE TABLE[^(]*"
    r"|<Route\b[^>]*path=[^>]*>)"
)


def code_map(root: Path, limit: int = 20000) -> str:
    """Every source file with its line count and top-level declarations (exports, functions, routes, tables).

    Given to each new session so it starts with the layout of the code instead of re-reading it."""
    lines: list[str] = []
    for part in SOURCE_DIRS:
        for path in sorted((root / part).rglob("*")) if (root / part).is_dir() else []:
            if not path.is_file() or path.suffix not in SOURCE_TYPES or "node_modules" in path.parts:
                continue
            text = path.read_text(errors="replace")
            found = [m.group(0).strip()[:120] for line in text.splitlines() if (m := SYMBOL.match(line))]
            lines.append(f"{path.relative_to(root)} ({text.count(chr(10)) + 1} lines)")
            lines.extend(f"    {item}" for item in dict.fromkeys(found[:25]))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n... (map truncated; use grep for the rest)"


def new_llm(model: str | None = None) -> LLM:
    return LLM(model)


MAX_HELPERS = int(os.environ.get("AGENT_HELPERS", "4"))
HELPER_STEPS = 25  # a subagent is asked to wrap up here; the lead can continue it with send_subagent
REVIEW_STEPS = 10  # a review: longer reviews cost more without finding more defects
WRAP_UP = (
    "You have used {steps} steps. Wrap up now: finish the item you are on in a few steps, then "
    "call done with your findings, every file you changed and what is left; the lead can continue you."
)

SUBAGENT = {
    "name": "subagent",
    "description": f"""Start a subagent in this workspace with a fresh context: \
only its task and the shared instructions. Returns at once; its final answer arrives later as a message. Up to \
{MAX_HELPERS} run at once; subagents cannot start their own.""",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["work", "review"],
                "description": "review: check existing code against requirements (short step "
                "budget); work: implementation, investigation or fact gathering.",
            },
            "task": {
                "type": "string",
                "description": "The goal, the requirement ids and files involved, the files "
                "it may change, and the output to report.",
            },
            "reasoning_effort": {
                "type": "string",
                "enum": ["low", "high", "max"],
                "description": "low: finding and summarizing; high: building and testing; "
                "max: hard diagnosis. Reviews always use max.",
            },
        },
        "required": ["task", "reasoning_effort", "kind"],
    },
}
SEND = {
    "name": "send_subagent",
    "description": "Continue a subagent's task with a correction or next step. A "
    "running subagent receives the message after its current step; a finished one resumes its own conversation "
    "(needs a free slot) and answers again as a message. Returns at once.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Subagent number, as returned by the subagent tool."},
            "message": {"type": "string", "description": "The instructions; the subagent keeps what it already knows."},
        },
        "required": ["id", "message"],
    },
}
WAIT = {
    "name": "wait_subagent",
    "description": "Block until a subagent finishes and return its final answer. "
    "Results otherwise arrive as messages by themselves; wait only when your next step needs the result.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "Subagent number, as returned by the subagent tool."}},
        "required": ["id"],
    },
}
CHECK = {
    "name": "check",
    "description": "Build the frontend, start the backend with npm start on a fresh "
    "database (and on a copy of the earlier stage's data when present) and run the Playwright tests in "
    "backend/test-e2e on each, 4 workers, 15 s per test. Failed tests are rerun one at a time and those that "
    "pass are reported as flaky. One check runs at a time. The report starts with CHECK PASSED or "
    "CHECK FAILED and lists build errors, failed tests with their errors and page, browser errors and rule "
    "violations.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Test files whose path matches, e.g. 'sort.spec' or "
                "'sort.spec|filter.spec'. Empty: the whole suite.",
            }
        },
    },
}
REQUIREMENT = {
    "name": "requirement",
    "description": "Full text of atomic requirements: the description (the "
    "acceptance rule), exact UI strings, reference image paths, acceptance scenarios, and the rules of "
    "the groups they belong to. Ask for all the ids of a feature area in one call.",
    "parameters": {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Atomic requirement ids from the outline, e.g. ["REQ-1-1-1", "REQ-1-2"].',
            }
        },
        "required": ["ids"],
    },
}

SUBAGENT_ROLE = """## Your role
You are a subagent started by the lead engineer for the task at the end of this message. The lead and other \
subagents work in the same workspace at the same time, so change only the files your task covers, and follow \
any further messages from the lead. The subagent tools belong to the lead. You may start the backend or a \
browser script for an experiment inside one bash command, on a free port; its processes end with the command.
Workspace root: {workspace}. Every bash call starts here; use relative paths.
Read everything you need in one reply. Call done when the task, or a coherent part of a large one, is finished: \
its summary is your answer to the lead, so make it short and complete (findings with file paths and exact \
names, every file you changed, what is left). The lead can continue you, so stopping early loses nothing.

A review compares the user interface the code renders with the requirement text and reports every difference \
as a defect: accessible names and messages that differ from the quoted strings in any character, wrong roles or \
element types, missing label associations, wrong default values or option lists, controls in the wrong region, \
and flows that need inputs the requirement does not name. The requirement wins over engineering rules, and a \
passing test proves nothing when it is looser than the requirement. Fix what you find in the files the task \
names unless it says report only, add no tests, and run check with the area's pattern after changing a file. \
Report each defect as file:line, what is there now and what the requirement demands, quoting it.

## Task of subagent {number}
{task}"""


class Helpers:
    """Subagents started with the subagent tool. They never block the lead: results arrive as messages, or on
    request with wait_subagent. send_subagent reaches a running subagent after its current step, and continues a
    finished one in its own conversation."""

    def __init__(self, root: Path, req_dir: Path, label: str, extras: dict, deadline: float | None = None):
        self.root, self.req_dir, self.label, self.extras = root, req_dir, label, extras
        self.pool = futures.ThreadPoolExecutor(max_workers=MAX_HELPERS)
        self.jobs: dict[int, tuple[str, futures.Future]] = {}
        self.delivered: set[int] = set()
        self.lock = threading.Lock()  # guards inbox and active: a message never lands after a subagent's last look
        self.inbox: dict[int, list[str]] = {}
        self.active: set[int] = set()  # subagents that still take messages in their running session
        self.sessions: dict[int, tuple[LLM, list[dict], int]] = {}  # model, final messages and turn of finished ones
        self.roles: dict[int, str] = {}  # each subagent's role and task: its first message again after a compaction
        self.budgets: dict[int, int] = {}  # the step at which each subagent is asked to wrap up
        self.stop = threading.Event()  # set when the lead's session ends
        self.deadline = deadline  # subagents end at the lead's time limit too
        # numbering continues after a resume, so labels and transcript files stay unique
        found = [
            int(m[1])
            for f in (root / checkpoint.AGENT_DIR).glob(f"{label}-helper*.jsonl")
            if (m := re.match(rf"{re.escape(label)}-helper(\d+)", f.name))
        ]
        self.first = max(found, default=0) + 1

    def start(self, task: str, effort: str = "", kind: str = "work") -> int:
        number = self.first + len(self.jobs)
        llm = new_llm()
        effort = "max" if kind == "review" else effort
        if effort in EFFORTS:
            llm.effort = effort
        self.roles[number] = SUBAGENT_ROLE.format(number=number, task=task, workspace=self.root)
        self.budgets[number] = REVIEW_STEPS if kind == "review" else HELPER_STEPS
        text = self.roles[number]
        with self.lock:
            self.active.add(number)
        self.jobs[number] = (task, self.pool.submit(self.work, number, llm, text, None, 1))
        log(f"{self.label}:helper{number}", f"started: {task[:200]}")
        return number

    def work(self, number: int, llm: LLM, text: str, history: list[dict] | None, turn: int) -> str:
        """Run the subagent until it calls done with no message from the lead waiting."""
        while True:
            label = f"{self.label}:helper{number}" + (f"-{turn}" if turn > 1 else "")
            final: list[dict] = []
            try:
                summary = loop.run(
                    llm,
                    ENGINEER,
                    text,
                    Tools(self.root, readable=[self.req_dir]),
                    extras=self.extras,
                    max_steps=HELPER_STEPS,
                    hard_limit=60,
                    label=label,
                    history=history,
                    final=final,
                    on_step=lambda step, _messages: self.take(number, step),
                    refresh=lambda: (self.roles[number], ""),
                    transcript=checkpoint.Transcript(self.root, label, commits=False),
                    deadline=self.deadline,
                )
            except BaseException:  # a failed subagent takes no more messages
                with self.lock:
                    self.active.discard(number)
                    self.inbox.pop(number, None)
                raise
            with self.lock:
                pending = self.inbox.pop(number, [])
                if not pending or self.stop.is_set():
                    self.active.discard(number)
                    self.sessions[number] = (llm, final, turn)
                    return summary
            history, text, turn = final, follow_up(pending), turn + 1

    def take(self, number: int, step: int = 0) -> str | None:
        """Messages from the lead waiting for subagent `number`, and the wrap-up request once its step budget is
        used, as one user message. Raises once the lead's session has ended, so the subagent changes no more files."""
        if self.stop.is_set():
            raise RuntimeError("the lead's session ended")
        with self.lock:
            pending = self.inbox.pop(number, [])
        parts = ([follow_up(pending)] if pending else []) + (
            [WRAP_UP.format(steps=step)] if step == self.budgets.get(number, HELPER_STEPS) else []
        )
        return "\n\n".join(parts) or None

    def full(self) -> str:
        """The error for a new or continued subagent when every slot is taken, else empty."""
        if len(self.running()) >= MAX_HELPERS:
            return f"ERROR: {MAX_HELPERS} subagents are already running; wait for one with wait_subagent"
        return ""

    def send(self, number: int, message: str) -> str:
        with self.lock:
            if number in self.active:
                self.inbox.setdefault(number, []).append(message)
                self.jobs[number] = (message, self.jobs[number][1])  # the latest instruction, for listings
                return f"Message queued for subagent {number}; it receives it after its current step."
        if error := self.full():
            return error
        if number not in self.sessions:
            return f"ERROR: subagent {number} ended without a conversation to continue (it failed); start a new one"
        previous = "" if number in self.delivered else f"\n\nIts previous final answer:\n{self.result(number)}"
        llm, history, turn = self.sessions.pop(number)
        with self.lock:
            self.active.add(number)
        self.delivered.discard(number)
        self.jobs[number] = (message, self.pool.submit(self.work, number, llm, follow_up([message]), history, turn + 1))
        log(f"{self.label}:helper{number}", f"continued: {message[:200]}")
        return f"Subagent {number} continues with your message; its final answer will arrive as a message.{previous}"

    def result(self, number: int) -> str:
        task, future = self.jobs[number]
        self.delivered.add(number)
        try:
            summary = future.result()
        except Exception as error:  # the lead decides what to do without it
            summary = f"failed: {type(error).__name__}: {error}"
        return f"Subagent {number} finished (task: {task[:160]}). Final answer:\n{summary}"

    def finished(self) -> list[str]:
        """Results that arrived since the last call."""
        ready = [n for n, (_, f) in self.jobs.items() if f.done() and n not in self.delivered]
        return [self.result(n) for n in ready]

    def running(self) -> list[int]:
        return [n for n, (_, f) in self.jobs.items() if not f.done()]

    def close(self) -> None:
        """Stop the subagents: a running one stops at its next step boundary (see take), and close waits for that,
        so no file changes after the lead's session ends."""
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)


def follow_up(messages: list[str]) -> str:
    return "\n\n".join(f"Message from the lead engineer:\n{m}" for m in messages)


WRAP_UP_SECONDS = 15 * 60  # before the time limit: time for one whole-suite check and its fixes
TIME_UP = (
    "The time limit is {minutes} minutes away; the build then ends and the workspace is delivered as it is. "
    "Start no new areas or subagents. Finish the change in hand, run check without a pattern, fix only what "
    "it reports, then call done."
)
RESUMED = (
    "The build was interrupted and resumes here. The subagents started before the interruption are gone: "
    "their results will not arrive and send_subagent cannot reach them. Check what they changed (git status, "
    "git diff) and start new subagents for their unfinished work."
)
CHECK_LOCK = threading.Lock()  # one build and test run at a time: they share frontend/dist


class Engineer:
    """The session that builds the app, with a check tool, requirement lookup and helpers."""

    def __init__(
        self,
        llm: LLM,
        root: Path,
        req_dir: Path,
        tree: spec.Node,
        label: str,
        steps: int,
        task_builder=None,
        status: dict[str, str] | None = None,
        publish=None,
        deadline: float | None = None,
    ):
        self.llm, self.root, self.req_dir, self.tree, self.label = llm, root, req_dir, tree, label
        self.steps, self.task_builder = steps, task_builder
        self.publish = publish
        self.atomics = {a.id: a for a in tree.atomics}
        self.status = status or {}  # requirements a later stage must check against the app: new, changed, unverified
        self.read: set[str] = set()  # ids whose text a session got through the requirement tool
        self.seeds = spec.seed_values(tree)
        self.last_report = ""
        self.built: list[str] = []  # snapshot commit of the last successful build
        self.skills = skills()
        self.helpers = Helpers(root, req_dir, label, self.extras(lead=False), deadline)
        self.settled = False  # the whole suite passed since the last compaction check
        self.deadline = deadline  # time.time() at which the build ends; None: no time limit
        self.warned = False

    def check(self, args: dict, lead: bool = True) -> str:
        pattern = str(args.get("pattern") or "").strip()
        pattern = "" if pattern.lower() == "all" else pattern  # as apply_patch's check argument
        with CHECK_LOCK:
            ok, report = checks.check(self.root, pattern, self.seeds)
            if lead and report.startswith("build ok"):  # a snapshot of the working tree, without touching it
                found = re.findall(r"\b[0-9a-f]{40}\b", shell("git stash create", self.root, 60)) or re.findall(
                    r"\b[0-9a-f]{40}\b", shell("git rev-parse HEAD", self.root, 30)
                )
                self.built = found[:1]
            elif lead and self.built and report.startswith("frontend build failed"):
                report += (
                    f"\nThe frontend last built at snapshot {self.built[0][:12]}; `git diff "
                    f"{self.built[0][:12]} -- frontend/src` shows every change since then."
                )
        if lead:
            self.last_report = report
            self.settled = ok and not pattern
        return ("CHECK PASSED\n" if ok else "CHECK FAILED\n") + report

    def requirement(self, args: dict, messages: list[dict], _call_id: str = "") -> str:
        wanted = [str(i) for i in args.get("ids") or []]
        unknown = [i for i in wanted if i not in self.atomics]
        if unknown or not wanted:
            return f"ERROR: unknown ids {unknown}; atomic ids are: {', '.join(self.atomics)}"
        self.read.update(wanted)
        rules = spec.shared(self.tree, wanted)
        cards = "\n\n".join(spec.card(self.atomics[i], str(self.req_dir)) for i in wanted)
        text = (f"## Rules of the groups these requirements belong to\n{rules}\n\n" if rules else "") + cards
        attached = [
            s.text
            for name, s in self.skills.items()
            if s.triggers and s.triggers.search(text) and not has_skill(messages, name)
        ]
        if attached:  # the skills these requirements need, once per conversation
            text += "\n\n## Skills these requirements need (follow them; the requirement text wins)\n\n" + "\n\n".join(
                attached
            )
        return text

    def skill(self, args: dict) -> str:
        names = [str(n) for n in args.get("names") or []]
        unknown = [n for n in names if n not in self.skills]
        if unknown or not names:
            return f"ERROR: unknown skills {unknown}; skills are: {', '.join(self.skills)}"
        return "\n\n".join(self.skills[n].text for n in names)

    def subagent(self, args: dict) -> str:
        if args.get("kind") not in ("work", "review"):
            return "ERROR: kind must be work (implementation/investigation) or review (audit/code review)"
        if error := self.helpers.full():
            return error
        if self.wrapping_up():
            return "ERROR: the time limit is near; finish the work in hand yourself, run check and call done"
        number = self.helpers.start(str(args.get("task", "")), str(args.get("reasoning_effort", "")), args["kind"])
        return f"Subagent {number} started; its final answer will arrive as a message. Keep working."

    def send_subagent(self, args: dict) -> str:
        number, message = int(args.get("id", 0)), str(args.get("message", "")).strip()
        if number not in self.helpers.jobs:
            return f"ERROR: no subagent {number}"
        if not message:
            return "ERROR: message is empty"
        if self.wrapping_up():
            return "ERROR: the time limit is near; finish the work in hand yourself, run check and call done"
        return self.helpers.send(number, message)

    def wait_subagent(self, args: dict) -> str:
        number = int(args.get("id", 0))
        if number not in self.helpers.jobs:
            return f"ERROR: no subagent {number}"
        return self.helpers.result(number)

    def wrapping_up(self) -> bool:
        return self.deadline is not None and time.time() >= self.deadline - WRAP_UP_SECONDS

    def on_step(self, _step: int, _messages: list[dict]) -> str | None:
        """Subagent results that arrived since the last step, and the wrap-up request once near the time limit."""
        if self.publish:
            self.publish()
        parts = self.helpers.finished()
        if self.wrapping_up() and not self.warned and self.deadline is not None:
            self.warned = True
            parts.append(TIME_UP.format(minutes=max(0, round((self.deadline - time.time()) / 60))))
        return "\n\n".join(parts) or None

    def refuse_done(self) -> str | None:
        running = self.helpers.running()
        if running:
            return (
                f"ERROR: done refused: subagents {', '.join(map(str, running))} are still running and would keep "
                "changing files after the build ends. Wait for them with wait_subagent, or keep working until "
                "their results arrive as messages, then call done."
            )
        unread = [i for i in self.status if i not in self.read]
        if unread:
            return (
                f"ERROR: done refused: {len(unread)} requirements marked new, changed or unverified have not been "
                f"compared with the app yet: {', '.join(unread[:60])}. Get their text with the requirement tool, "
                "fix every difference, run check without a pattern, then "
                "call done."
            )
        return None

    def milestone(self) -> bool:
        settled, self.settled = self.settled, False
        return settled

    def refresh(self) -> tuple[str | None, str]:
        """Fresh task text and the facts a compaction must carry over verbatim."""
        facts = [f"## Latest check report\n{self.last_report[:8000]}"] if self.last_report else []
        running = [f"- subagent {n}: {self.helpers.jobs[n][0][:300]}" for n in self.helpers.running()]
        if running:
            facts.append("## Subagents still running (their results will arrive as messages)\n" + "\n".join(running))
        return (self.task_builder() if self.task_builder else None), "\n\n".join(facts)

    def extras(self, lead: bool) -> dict[str, loop.Extra]:
        """Tools for each role; subagents cannot start subagents of their own."""

        def refused(*_args) -> str:
            return "ERROR: only the lead engineer can use this tool; report what it should do in your answer"

        return {
            "check": loop.Extra(CHECK, lambda args: self.check(args, lead)),
            "requirement": loop.Extra(REQUIREMENT, self.requirement, context=True),
            "skill": loop.Extra(skill_schema(self.skills), self.skill),
            "subagent": loop.Extra(SUBAGENT, self.subagent if lead else refused),
            "send_subagent": loop.Extra(SEND, self.send_subagent if lead else refused),
            "wait_subagent": loop.Extra(WAIT, self.wait_subagent if lead else refused),
        }

    def run(self, task: str, resume: int | str | None = None) -> str:
        extras = self.extras(lead=True)
        try:
            tools = Tools(self.root, readable=[self.req_dir], experiments=False)
            return loop.run(
                self.llm,
                ENGINEER,
                task,
                tools,
                extras=extras,
                max_steps=self.steps,
                on_step=self.on_step,
                label=self.label,
                transcript=checkpoint.Transcript(self.root, self.label),
                resume=resume,
                note=RESUMED,
                refresh=self.refresh,
                milestone=self.milestone,
                refuse_done=self.refuse_done,
                deadline=self.deadline,
            )
        finally:
            self.helpers.close()
