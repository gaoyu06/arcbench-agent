"""One agent session: the model calls tools until it calls `done` or runs out of steps."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import checkpoint
import compaction
from checkpoint import Transcript
from llm import LLM
from tools import Tools, is_read_only

DONE = {
    "name": "done",
    "description": "Finish this task and end the session; other tool calls in the same "
    "reply after done are not run. The lead calls it after a whole-suite check passes, or when no further "
    "progress is possible; a subagent calls it when its task is finished (reviews and investigations need no "
    "check).",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "What was built or found, the last check result, and "
                "what is still missing. A subagent's summary is its final answer to the lead.",
            }
        },
        "required": ["summary"],
    },
}


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
DESCRIBE = (
    "Describe this UI screenshot for a developer who must rebuild it: overall layout, every visible "
    "control with its exact label text, headings, lists and table structure, and visible states."
)
_visual: dict[str, LLM] = {}
_visual_lock = threading.Lock()


def view_image(llm: LLM, tools: Tools, arguments: str) -> tuple[str, dict | None]:
    """Attach the image for a vision model, otherwise describe it with the visual model."""
    path = json.loads(arguments or "{}").get("path", "")
    mime = IMAGE_TYPES.get(Path(path).suffix.lower())
    if not mime:
        return f"ERROR: {path} is not an image ({', '.join(IMAGE_TYPES)}); read text files with bash", None
    data = base64.b64encode(tools._path(path, write=False).read_bytes()).decode()
    image = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
    if llm.vision:
        return (
            f"Image {path} is attached in the next message.",
            {"role": "user", "content": [{"type": "text", "text": f"Image {path}:"}, image]},
        )
    with _visual_lock:
        if "llm" not in _visual:
            _visual["llm"] = LLM.visual()
            _visual["llm"].label = "visual"
    visual = _visual["llm"]
    reply = visual.chat(
        "You describe images accurately.",
        [{"role": "user", "content": [{"type": "text", "text": DESCRIBE}, image]}],
        [],
    )
    if not reply.text.strip():
        return f"ERROR: the vision model returned no description of {path}; rely on the requirement text", None
    return f"Description of image {path} (from a vision model):\n{reply.text}", None


def image_call(llm: LLM, tools: Tools, arguments: str) -> tuple[str, dict | None]:
    """Result of a view_image call and an optional image attachment."""
    try:
        return view_image(llm, tools, arguments)
    except Exception as error:
        return f"ERROR: {type(error).__name__}: {error}", None


def read_only(call: dict) -> bool:
    if call["name"] == "view_image":
        return True
    if call["name"] != "bash":
        return False
    try:
        return is_read_only(json.loads(call["arguments"] or "{}").get("command", ""))
    except ValueError:
        return False


def prefetch_reads(llm: LLM, tools: Tools, calls: list[dict]) -> dict[str, tuple[str, dict | None]]:
    """Run the read-only calls (images, read-only shell commands) that precede the first other call of a
    reply concurrently; they cannot observe that call's effects, so the result equals sequential execution."""
    leading = []
    for call in calls:
        if not read_only(call):
            break
        leading.append(call)
    if len(leading) < 2:
        return {}

    def execute(call: dict) -> tuple[str, dict | None]:
        if call["name"] == "view_image":
            return image_call(llm, tools, call["arguments"])
        return tools.run(call["name"], call["arguments"]), None

    with ThreadPoolExecutor(max_workers=min(8, len(leading))) as pool:
        return {call["id"]: result for call, result in zip(leading, pool.map(execute, leading), strict=True)}


def requested_check(arguments: str) -> str | None:
    """The spec pattern of an apply_patch call's `check` argument ('' for the whole suite), or None."""
    try:
        pattern = json.loads(arguments or "{}").get("check")
    except (ValueError, AttributeError):
        return None
    if not isinstance(pattern, str) or pattern.strip().lower() in ("", "false", "none", "no", "off", "null"):
        return None
    return "" if pattern.strip().lower() == "all" else pattern.strip()


@dataclass
class Extra:
    schema: dict
    run: Callable[..., str]
    context: bool = False  # run(args, messages, call_id): also receives the conversation and the call's id


HARD_LIMIT_FACTOR = 4  # guard against a session that never converges
REREAD_MIN_CHARS = 600  # shorter outputs cost less than the note that replaces them


def run(
    llm: LLM,
    system: str,
    task: str,
    tools: Tools,
    *,
    extras: dict[str, Extra] | None = None,
    max_steps: int = 60,
    label: str = "",
    transcript: Transcript | None = None,
    resume: int | str | None = None,
    note: str | None = None,
    on_step: Callable[[int, list[dict]], str | None] | None = None,
    refuse_done: Callable[[], str | None] | None = None,
    refresh: Callable[[], tuple[str | None, str]] | None = None,
    hard_limit: int | None = None,
    milestone: Callable[[], bool] | None = None,
    deadline: float | None = None,
    history: list[dict] | None = None,
    final: list[dict] | None = None,
) -> str:
    """Returns the model's final summary.

    With a transcript every message is recorded and each step ends with a workspace commit. `resume`
    ("last" or a step number) continues from that checkpoint: files are reset to its commit, later transcript
    records are archived and `note` is added to the conversation. `history` starts the conversation with earlier
    messages (a forked session), so the provider's prompt cache covers them. `milestone` reports (once) that the
    work so far is settled, which lets a moderately long history be compacted early. `final` receives the
    conversation as it ends, so the session can be continued later. `refuse_done` returns why a done call is
    refused, or None. At `deadline` (a time.time() value) the session ends like at the step limit.
    """
    extras = extras or {}
    schemas = [
        {"type": "function", "function": s} for s in [*tools.schemas, *(e.schema for e in extras.values()), DONE]
    ]
    llm.label = label
    messages: list[dict] = []
    summary_state: dict = {}
    first = 1
    loaded = None
    if transcript and resume is not None:
        loaded = transcript.load(None if resume == "last" else int(resume))
    if transcript and not loaded:
        transcript.archive()  # a fresh session never appends to an earlier attempt's records
    if transcript and loaded:
        messages, summary_state, step, sha = loaded
        checkpoint.reset(tools.root, sha)
        transcript.truncate(step)
        first = step + 1
        print(f"[{label}] resumed at step {step} ({len(messages)} messages, commit {sha[:8]})", flush=True)

    def add(message: dict) -> None:
        messages.append(message)
        if transcript:
            transcript.message(message)

    if not messages:
        messages.extend(history or [])
        add({"role": "user", "content": task})
    elif note:
        add({"role": "user", "content": note})
    started = time.time()
    hard_limit = hard_limit or max_steps * HARD_LIMIT_FACTOR
    for step in range(first, hard_limit + 1):
        if deadline and time.time() >= deadline:
            print(f"[{label}] time limit reached at step {step}", flush=True)
            break
        messages = compaction.trim(messages)
        compacted = compaction.compact(
            llm, system, schemas, messages, summary_state, refresh, milestone=bool(milestone and milestone())
        )
        if compacted is not messages:
            messages = compacted
            if transcript:
                transcript.compaction(messages, summary_state)
        reply = llm.chat(system, messages, schemas)
        add(reply.message)
        if not reply.tool_calls:
            add({"role": "user", "content": "Continue with tool calls, or call done if finished."})
        attachments = []
        prefetched = prefetch_reads(llm, tools, reply.tool_calls)
        for call in reply.tool_calls:
            name = call["name"]
            refusal = refuse_done() if name == "done" and refuse_done else None
            if name == "done" and not refusal:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except ValueError:
                    args = {"summary": call["arguments"]}
                add({"role": "tool", "tool_call_id": call["id"], "content": "finished"})
                if transcript:
                    transcript.checkpoint(step, len(messages), label)
                if final is not None:
                    final[:] = messages
                print(
                    f"[{label}] done in {step} steps, {time.time() - started:.0f}s: {args.get('summary', '')[:300]}",
                    flush=True,
                )
                return args.get("summary", "")
            elif refusal:
                result = refusal
            elif name in extras:
                try:
                    args = json.loads(call["arguments"] or "{}")
                    extra = extras[name]
                    result = extra.run(args, messages, call["id"]) if extra.context else extra.run(args)
                except Exception as error:
                    result = f"ERROR: {type(error).__name__}: {error}"
            elif call["id"] in prefetched:
                result, attachment = prefetched.pop(call["id"])
                if attachment:
                    attachments.append(attachment)
            elif name == "view_image":
                result, attachment = image_call(llm, tools, call["arguments"])
                if attachment:
                    attachments.append(attachment)
            else:
                result = tools.run(name, call["arguments"])
                pattern = requested_check(call["arguments"]) if name == "apply_patch" else None
                if pattern is not None and not result.startswith("Success"):
                    result += "\n\nThe check was not run because the patch did not apply completely."
                elif pattern is not None:  # the check the model would otherwise call in its next reply
                    try:
                        report = extras["check"].run({"pattern": pattern})
                    except Exception as error:
                        report = f"ERROR: {type(error).__name__}: {error}"
                    # its own log line: the patch line below is cut at 160 characters
                    print(
                        f"[{label}] {step} check {json.dumps({'pattern': pattern})!r} -> {report[:160]!r}", flush=True
                    )
                    result += f"\n\n{report}"
            if (
                name == "bash"
                and read_only(call)
                and len(result) > REREAD_MIN_CHARS
                and any(m.get("role") == "tool" and m.get("content") == result for m in messages)
            ):
                result = (
                    "Unchanged: this exact output is already in the conversation from an earlier command; use that."
                )
            print(f"[{label}] {step} {name} {call['arguments'][:120]!r} -> {result[:160]!r}", flush=True)
            add({"role": "tool", "tool_call_id": call["id"], "content": result})
        for attachment in attachments:  # user messages may only follow the complete batch of tool results
            add(attachment)
        if getattr(reply, "finish", "") == "length":
            add(
                {
                    "role": "user",
                    "content": "Your reply reached the output token limit and was cut off, so its "
                    "last tool call is incomplete. Send large files in several smaller patches (Add File with the "
                    "first part, then Update File sections of only + lines ended by *** End of File, which append "
                    "to the file) and keep reasoning brief.",
                }
            )
        advice = on_step(step, messages) if on_step else None
        if advice:
            add({"role": "user", "content": advice})
        if transcript:
            transcript.checkpoint(step, len(messages), label)
    if final is not None:
        final[:] = messages
    print(f"[{label}] step or time limit reached after {time.time() - started:.0f}s", flush=True)
    return "step or time limit reached"
