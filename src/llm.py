"""OpenAI-compatible chat client with DeepSeek thinking support, retries and usage accounting."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import APIStatusError, OpenAI


class FatalModelError(RuntimeError):
    """Credentials, quota or a request the provider will never accept."""


USAGE_LOG: Path | None = None  # one JSON line per model call when set (see usage_report)
_log_lock = threading.Lock()


@dataclass
class Usage:
    calls: int = 0
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    reasoning: int = 0

    def add(self, usage) -> dict:
        if not usage:
            return {}
        details = getattr(usage, "prompt_tokens_details", None)
        out_details = getattr(usage, "completion_tokens_details", None)
        record = {
            "prompt": usage.prompt_tokens or 0,
            "completion": usage.completion_tokens or 0,
            "cached": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
            "reasoning": (getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0,
        }
        self.calls += 1
        self.prompt += record["prompt"]
        self.cached += record["cached"]
        self.completion += record["completion"]
        self.reasoning += record["reasoning"]
        return record


def usage_report(path: Path) -> str:
    """Totals per model and per session role from a usage log."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    if not rows:
        return "no model calls recorded"

    def role(label: str) -> str:
        if ":helper" in label:
            return "helper"
        return "visual" if label == "visual" else "engineer"

    lines = []
    for title, key in (("model", lambda r: r["model"]), ("role", lambda r: role(r["label"]))):
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(key(row), []).append(row)
        for name, items in sorted(groups.items()):
            prompt, cached = sum(r["prompt"] for r in items), sum(r["cached"] for r in items)
            lines.append(
                f"{title}={name}: calls={len(items)} prompt={prompt} cached={cached} "
                f"({100 * cached // max(prompt, 1)}%) completion={sum(r['completion'] for r in items)} "
                f"reasoning={sum(r['reasoning'] for r in items)} seconds={sum(r['seconds'] for r in items):.0f}"
            )
    return "\n".join(lines)


@dataclass
class Reply:
    message: dict  # assistant message, replayable verbatim (keeps reasoning_content)
    tool_calls: list[dict] = field(default_factory=list)
    text: str = ""
    finish: str = ""


# Models known to accept image input; MODEL_VISION=1/0 overrides for anything else.
VISION_MODELS = ("glm-5.3-flash", "glm-5.3", "deepseek-v4-flash-vision-exp")


def supports_vision(model: str) -> bool:
    override = os.environ.get("MODEL_VISION")
    if override in ("0", "1"):
        return override == "1"
    return model in VISION_MODELS or "vision" in model


DEFAULT_MODEL = (
    os.environ.get("MODEL") or "deepseek-v4-flash-vision-exp"
)  # the model of every session but the visual one
# GLM and Kimi have a window of about 256K tokens (input and output together); DeepSeek's is far larger. The
# output limit leaves room for the compaction trigger (compaction.TRIGGER_TOKENS) in a small window.
SMALL_WINDOW = DEFAULT_MODEL.startswith(("glm", "kimi"))
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "65536" if SMALL_WINDOW else "131072"))
REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT", "high")  # a subagent may be given another level
EFFORTS = ("low", "high", "max")  # DeepSeek maps medium to high
# Transient failures (connection errors, timeouts, 5xx, rate limits) are retried for this long before the run
# gives up and delivers its current state; credential and quota errors stop immediately.
RETRY_SECONDS = int(os.environ.get("AGENT_RETRY_SECONDS", "900"))
# A stream cut off after it delivered tokens is the gateway dropping a long reply, not an outage: it is sent again
# at once with an output limit below the cut and, from the second cut on, low reasoning effort, up to
# BROKEN_STREAMS times.
BROKEN_STREAMS = int(os.environ.get("AGENT_BROKEN_STREAMS", "10"))
# Replies are streamed; a connection that delivers nothing for this long is dropped and the request retried.
# The platform's GLM route sends its first token after 12-45 s and the reply in bursts, so its waits are longer.
SLOW_START = DEFAULT_MODEL.startswith("glm")
IDLE_SECONDS = int(os.environ.get("AGENT_IDLE_SECONDS", "120" if SLOW_START else "60"))
# Platform latency swings widely, and a request that is stuck before its first token usually stays stuck. When no
# token (reasoning, answer or tool call) has arrived HEDGE_SECONDS after sending, the same request is sent once
# more and whichever stream produces a token first is used; the other is closed. When neither has produced one
# after FIRST_TOKEN_SECONDS, both are dropped and the request is retried at once.
HEDGE_SECONDS = float(os.environ.get("AGENT_HEDGE_SECONDS", "60" if SLOW_START else "12"))
FIRST_TOKEN_SECONDS = float(os.environ.get("AGENT_FIRST_TOKEN_SECONDS", "150" if SLOW_START else "30"))


class FirstTokenTimeout(TimeoutError):
    pass


def _has_token(chunk) -> bool:
    for choice in getattr(chunk, "choices", None) or []:
        delta = choice.delta
        if (
            delta.content
            or delta.tool_calls
            or getattr(delta, "reasoning_content", None)
            or (delta.model_extra or {}).get("reasoning_content")
        ):
            return True
    return False


class _Stream:
    """One request, read in a background thread into a queue; `abandon()` makes the thread close it."""

    END = object()

    def __init__(self, open_stream):
        self.chunks: queue.Queue = queue.Queue()
        self.abandoned = threading.Event()
        threading.Thread(target=self._read, args=(open_stream,), daemon=True).start()

    def _read(self, open_stream) -> None:
        try:
            stream = open_stream()
            for chunk in stream:
                if self.abandoned.is_set():
                    break
                self.chunks.put(chunk)
            stream.close()
            self.chunks.put(self.END)
        except Exception as error:
            self.chunks.put(error)

    def abandon(self) -> None:
        self.abandoned.set()


def paced(open_stream, timing: dict):
    """The chunks of one streamed request (see HEDGE_SECONDS). Raises FirstTokenTimeout when no stream produced a
    token in FIRST_TOKEN_SECONDS, TimeoutError when the chosen stream then stalls for IDLE_SECONDS, and the
    error of the request when every stream failed."""
    streams = [_Stream(open_stream)]
    chosen, held = None, []  # chunks without a token (role headers) seen before a stream was chosen
    try:
        while chosen is None:
            now = time.time()
            if now >= timing["sent"] + FIRST_TOKEN_SECONDS:
                raise FirstTokenTimeout(f"no token after {FIRST_TOKEN_SECONDS:.0f}s")
            if len(streams) == 1 and now >= timing["sent"] + HEDGE_SECONDS and not streams[0].abandoned.is_set():
                streams.append(_Stream(open_stream))
            progressed = False
            for stream in list(streams):
                try:
                    item = stream.chunks.get_nowait()
                except queue.Empty:
                    continue
                progressed = True
                if isinstance(item, BaseException):
                    stream.abandon()
                    streams.remove(stream)
                    if not streams:
                        raise item
                elif item is _Stream.END or _has_token(item):
                    chosen = stream
                    held.append(item)
                    break
                elif stream is streams[0]:
                    held.append(item)
            if not progressed:
                time.sleep(0.05)
        for stream in streams:
            if stream is not chosen:
                stream.abandon()
        for item in held:
            if item is _Stream.END:
                return
            yield item
        while True:
            try:
                item = chosen.chunks.get(timeout=IDLE_SECONDS)
            except queue.Empty:
                raise TimeoutError(f"no data for {IDLE_SECONDS}s") from None
            if item is _Stream.END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        for stream in streams:
            stream.abandon()


# Gateway workarounds. The competition gateway behaves unlike a plain OpenAI endpoint in two observed ways, and
# both are handled here only:
# - it reports a reset proxy connection as HTTP 400 with "proxy" in the body, so that 400 is retried;
# - it drops a reply that streams too long. Resending the same request would be cut again, so the resend asks for
#   an output limit well below the cut (about 4 characters per token, 60% of what arrived) and, from the second
#   cut on, low reasoning effort; the session then sees a "length" finish and is told to split its work.
def retryable(error: APIStatusError) -> bool:
    """Whether an HTTP error is transient. Credentials, quota and other client errors stop the run."""
    body = str(error.body or error.message).lower()
    if error.status_code in (401, 402, 403) or (error.status_code == 429 and "quota" in body):
        return False
    return error.status_code >= 500 or error.status_code == 429 or "proxy" in body


def shorten_after_cut(request: dict, received_chars: int, broken: int) -> None:
    if broken >= 2 and "extra_body" in request:
        request["extra_body"]["reasoning_effort"] = "low"
    cap = max(4096, int(received_chars / 4 * 0.6))
    if cap < (request.get("max_tokens") or cap + 1):
        request["max_tokens"] = cap


class LLM:
    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = MAX_TOKENS,
    ):
        self.model = model or DEFAULT_MODEL
        self.client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            timeout=IDLE_SECONDS,
            max_retries=0,
        )
        self.max_tokens = max_tokens
        self.vision = supports_vision(self.model)
        self.usage = Usage()
        self.effort = REASONING_EFFORT  # one of EFFORTS, or "" for the endpoint default
        self.label = ""  # session name recorded in the usage log

    @classmethod
    def visual(cls) -> LLM:
        """The platform's separate vision model, used when the main model cannot read images."""
        visual = cls(
            os.environ.get("VISUAL_MODEL") or "deepseek-v4-flash-vision-exp",
            api_key=os.environ.get("VISUAL_API_KEY"),
            base_url=os.environ.get("VISUAL_BASE_URL"),
            max_tokens=None,
        )
        visual.effort = "low"  # describing an image needs no long reasoning
        return visual

    def chat(self, system: str, messages: list[dict], tools: list[dict], tool_choice: str | None = None) -> Reply:
        request: dict[str, Any] = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages]}
        if self.max_tokens:  # None: the endpoint's own limit
            request["max_tokens"] = self.max_tokens
        if tools:
            request["tools"] = tools
            if tool_choice:
                request["tool_choice"] = tool_choice
        if self.model.startswith(("deepseek", "glm")):
            request["extra_body"] = {"thinking": {"type": "enabled"}}
            if self.effort:  # ignored by endpoints without the control
                request["extra_body"]["reasoning_effort"] = self.effort
        request.update(stream=True, stream_options={"include_usage": True})
        started = time.time()
        attempt = broken = 0
        retry: Exception
        timing: dict = {}

        def progress(text: str) -> None:
            timing["last"] = time.time()  # data arrived: the endpoint is alive
            timing.setdefault("first", timing["last"])
            timing["chars"] = timing.get("chars", 0) + len(text)

        while True:
            timing.update(sent=time.time(), chars=0)
            timing.pop("first", None)
            try:
                stream = paced(lambda: self.client.chat.completions.create(**request), timing)
                message, finish, usage = self._receive(stream, progress)
                break
            except APIStatusError as error:
                if not retryable(error):
                    raise FatalModelError(str(error)) from error
                retry = error
            except (AttributeError, KeyError, NameError, TypeError):  # a bug here, not a network problem
                raise
            except Exception as error:  # connection reset, idle timeout or a broken stream: send the request again
                retry = error
            received = timing.get("chars", 0)
            cut = "first" in timing  # the stream broke after delivering tokens
            if time.time() - max(started, timing.get("last", 0)) > RETRY_SECONDS:  # nothing received: an outage
                raise FatalModelError(f"model unavailable for {RETRY_SECONDS}s: {retry}")
            if cut:
                broken += 1
                if broken > BROKEN_STREAMS:
                    raise FatalModelError(f"{broken} streams broke off: {retry}")
                shorten_after_cut(request, received, broken)
            # a slow first token or a cut stream is the platform's latency, not an outage: send again at once
            wait = 1 if cut or isinstance(retry, FirstTokenTimeout) else min(60, 5 * 2**attempt)
            attempt += 1
            print(
                f"[llm] {type(retry).__name__}: {str(retry)[:200]}; {received} chars after "
                f"{time.time() - timing['sent']:.0f}s; retry in {wait}s"
                f"{' with low reasoning effort' if cut and broken >= 2 else ''}",
                flush=True,
            )
            time.sleep(wait)
        ended = time.time()
        record = self.usage.add(usage)
        if USAGE_LOG and record:
            first = timing.get("first")
            record.update(
                time=round(started, 1),
                seconds=round(ended - started, 1),
                model=self.model,
                label=self.label,
                effort=self.effort,
                retries=attempt,
                finish=finish,
                ttft=round(first - timing["sent"], 2) if first else None,
                tps=round(record["completion"] / (ended - first), 1) if first and ended > first else None,
            )
            with _log_lock, USAGE_LOG.open("a") as log:
                log.write(json.dumps(record) + "\n")
        calls = [
            {"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
            for c in message.get("tool_calls") or []
        ]
        return Reply(message, calls, message.get("content") or "", finish)

    @staticmethod
    def _receive(stream, progress=None) -> tuple[dict, str, object]:
        """Assemble a streamed reply into (assistant message, finish reason, usage). `progress(text)` is
        told about every streamed piece of reasoning, answer or tool call."""
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish, usage = "", None
        for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            for choice in chunk.choices or []:
                delta = choice.delta
                finish = choice.finish_reason or finish
                if delta.content:
                    content.append(delta.content)
                    if progress:
                        progress(delta.content)
                thought = getattr(delta, "reasoning_content", None) or (delta.model_extra or {}).get(
                    "reasoning_content"
                )
                if thought:
                    reasoning.append(thought)
                    if progress:
                        progress(thought)
                for part in delta.tool_calls or []:
                    if progress and part.function:
                        progress((part.function.name or "") + (part.function.arguments or ""))
                    call = calls.setdefault(
                        part.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    call["id"] = part.id or call["id"]
                    if part.function:
                        call["function"]["name"] += part.function.name or ""
                        call["function"]["arguments"] += part.function.arguments or ""
        message: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return message, finish, usage
