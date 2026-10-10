"""Context compaction modelled on Codex: the session writes its own handover summary, then its history is
replaced by [fresh task, summary, recent turns].

The summary request is the current conversation plus one instruction, so the provider's prompt cache
covers almost all of it and nothing is truncated. Between compactions history stays append-only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from collections.abc import Set as AbstractSet

from llm import LLM, SMALL_WINDOW, FatalModelError

TRIGGER_TOKENS = 150000 if SMALL_WINDOW else 400000
KEEP_RECENT_TOKENS = 50000 if SMALL_WINDOW else 60000
# At a milestone (the whole suite passes) the work behind it is settled, so a history past MILESTONE_TOKENS
# is compacted there instead of in the middle of the next piece of work.
MILESTONE_TOKENS = 130000 if SMALL_WINDOW else 340000
SUMMARY_TAG = "[Handover summary: an earlier part of this session was compacted]"

SUMMARY_REQUEST = """CONTEXT CHECKPOINT. Your earlier messages will be removed from the conversation and \
replaced by the summary you write now; another instance of you continues from it. Do not call tools; \
answer with the summary only. The continuation also receives the task again with the current plan and a fresh \
code map, the latest check report and the subagents still running, so do not copy those. Include results of \
subagents that are still relevant. Write:

## Progress
Each feature area of the plan with its requirement ids: done and passing / implemented but failing (why) / \
not started.
## Decisions
Design choices made and the reasons, including approaches tried and abandoned.
## Current problem
What is being worked on right now, the evidence (failing test, error text) and the current hypothesis.
## Next steps
Concrete, in order.
## Critical details
Exact UI strings, file paths, function names, commands and facts learned that are not visible in the code."""

IMAGE_TOKENS = 1500  # providers bill a screenshot at a few hundred to ~1.5k tokens, not its base64 size


def estimate(messages: list[dict]) -> int:
    """Conservative token estimate (code and JSON average about 3.5 characters per token)."""
    total = 0
    for m in messages:
        if isinstance(m.get("content"), list):
            images = sum(1 for p in m["content"] if p.get("type") == "image_url")
            text = [p for p in m["content"] if p.get("type") != "image_url"]
            total += images * IMAGE_TOKENS + len(json.dumps({**m, "content": text}, ensure_ascii=False)) * 10 // 35
        else:
            total += len(json.dumps(m, ensure_ascii=False)) * 10 // 35
    return total


def cut_index(messages: list[dict], keep_tokens: int) -> int:
    """Index of the first kept message: an assistant turn, so tool results stay with their call."""
    total, cut = 0, len(messages)
    for i in range(len(messages) - 1, 0, -1):
        total += estimate([messages[i]])
        if messages[i]["role"] == "assistant":
            cut = i
            if total >= keep_tokens:
                break
    return cut


TRIM_START_TOKENS = 100000 if SMALL_WINDOW else 200000
TRIM_KEEP_TOKENS = 50000 if SMALL_WINDOW else 60000
TRIM_MIN_GAIN = 30000
TRIMMED = "[trimmed from the context; read the file or run the command again if needed]"


PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)


def _paths(call: dict) -> set[str]:
    """Files an apply_patch call changes."""
    if call["function"]["name"] != "apply_patch":
        return set()
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except ValueError:
        return set()
    patch = args.get("input") if isinstance(args, dict) else None
    return {p.strip() for p in PATCH_FILE.findall(patch)} if isinstance(patch, str) else set()


def _trim_message(m: dict, rewritten: AbstractSet[str] = frozenset()) -> dict:
    """`m` without the bulky parts the model rarely needs again: long tool output, the content of files that
    were changed again later (`rewritten`) and screenshots. The content of a file not changed since stays: it
    is the model's record of what the file holds. Reasoning is passed back verbatim, as DeepSeek expects."""
    if m["role"] == "assistant":
        calls = []
        for call in m.get("tool_calls") or []:
            arguments = call["function"]["arguments"] or ""
            changed = _paths(call)
            if len(arguments) > 1500 and changed and changed <= rewritten:
                try:
                    args = json.loads(arguments)
                    args = {
                        k: (
                            f"{v[:120]} ... {TRIMMED}"
                            if isinstance(v, str) and len(v) > 300
                            else "[trimmed]"
                            if isinstance(v, list) and len(json.dumps(v)) > 300
                            else v
                        )
                        for k, v in args.items()
                    }
                    call = {**call, "function": {**call["function"], "arguments": json.dumps(args, ensure_ascii=False)}}
                except ValueError:
                    pass
            calls.append(call)
        return {**m, "tool_calls": calls} if calls else m
    # a loaded skill is guidance followed for the rest of the session, so it stays, also when attached to the
    # output of another tool (the requirement tool)
    if (
        m["role"] == "tool"
        and isinstance(m.get("content"), str)
        and len(m["content"]) > 600
        and not m["content"].startswith("---\nname: ")
    ):
        skills = m["content"].find("\n---\nname: ")
        kept = m["content"][skills:] if skills >= 0 else ""
        return {**m, "content": f"{m['content'][:200]}\n{TRIMMED}{kept}"}
    if m["role"] == "user" and isinstance(m.get("content"), list):
        text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
        return {**m, "content": f"{text}\n[screenshot trimmed from the context]".strip()}
    return m


def trim(messages: list[dict]) -> list[dict]:
    """Trim the bulky parts of turns before the most recent TRIM_KEEP_TOKENS once that saves TRIM_MIN_GAIN
    tokens. Every trim changes the history once, so the prompt cache is rebuilt only after each batch; short
    sessions (under TRIM_START_TOKENS) are left alone because the rebuilt cache would not pay off."""
    if estimate(messages) < TRIM_START_TOKENS:
        return messages
    cut = cut_index(messages, TRIM_KEEP_TOKENS)
    later: list[set[str]] = [set() for _ in messages]  # files changed by calls after each message
    seen: set[str] = set()
    for i in range(len(messages) - 1, -1, -1):
        later[i] = set(seen)
        for call in messages[i].get("tool_calls") or []:
            seen |= _paths(call)
    old = [_trim_message(m, later[i]) for i, m in enumerate(messages[1:cut], 1)]
    if estimate(messages[1:cut]) - estimate(old) < TRIM_MIN_GAIN:
        return messages
    return [messages[0], *old, *messages[cut:]]


def summarize(llm: LLM, system: str, tools: list[dict], messages: list[dict]) -> str:
    """The session's own summary of `messages`. If the request itself is too long for the model, the oldest
    turns after the task are dropped first, which keeps the cached prefix as long as possible."""
    history = list(messages)
    for _ in range(4):
        try:
            request = [*history, {"role": "user", "content": SUMMARY_REQUEST}]
            reply = llm.chat(system, request, tools, tool_choice="none")
            if not reply.text.strip():  # deepseek-v4-flash sometimes calls tools anyway; without tools it cannot
                reply = llm.chat(system, request, [])
            return reply.text.strip()
        except FatalModelError as error:
            if not any(word in str(error).lower() for word in ("context", "length", "too long", "maximum")):
                raise
            cut = cut_index(history, estimate(history) // 2)
            history = [history[0], *history[max(cut, 2) :]]
    return ""


def compact(
    llm: LLM,
    system: str,
    tools: list[dict],
    messages: list[dict],
    state: dict,
    refresh: Callable[[], tuple[str | None, str]] | None = None,
    milestone: bool = False,
) -> list[dict]:
    """New message list when the history exceeds the trigger (or, at a milestone, MILESTONE_TOKENS), else
    `messages` unchanged.

    `refresh` returns (fresh task text or None to keep the original, harness facts to attach verbatim)."""
    size = estimate(messages)
    if size <= (MILESTONE_TOKENS if milestone else TRIGGER_TOKENS):
        return messages
    cut = cut_index(messages, KEEP_RECENT_TOKENS)
    if cut <= 1:
        return messages
    summary = summarize(llm, system, tools, messages) or state.get("summary", "")
    task, facts = refresh() if refresh else (None, "")
    state["summary"] = summary
    first = {"role": "user", "content": task} if task else messages[0]
    note = f"{SUMMARY_TAG}\n{summary}" + (f"\n\n{facts}" if facts else "")
    new = [first, {"role": "user", "content": note}, *messages[cut:]]
    print(
        f"[compact]{' milestone' if milestone else ''} {size} -> {estimate(new)} tokens (replaced {cut - 1} messages)",
        flush=True,
    )
    return new
