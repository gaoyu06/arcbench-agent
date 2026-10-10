import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class FirstTokenTest(unittest.TestCase):
    def setUp(self):
        import llm

        self.saved = llm.HEDGE_SECONDS, llm.FIRST_TOKEN_SECONDS
        llm.HEDGE_SECONDS, llm.FIRST_TOKEN_SECONDS = 0.5, 1.5

    def tearDown(self):
        import llm

        llm.HEDGE_SECONDS, llm.FIRST_TOKEN_SECONDS = self.saved

    @staticmethod
    def chunk(text):
        from types import SimpleNamespace as N

        return N(
            choices=[
                N(delta=N(content=text, tool_calls=None, reasoning_content=None, model_extra={}), finish_reason=None)
            ],
            usage=None,
        )

    def test_a_stuck_request_is_hedged_and_a_silent_one_cut_short(self):
        import time as clock

        import llm

        calls = []

        def first_stuck():
            calls.append(1)
            if len(calls) == 1:
                clock.sleep(5)
            return (c for c in [self.chunk("a"), self.chunk("b")])

        started = clock.time()
        texts = [c.choices[0].delta.content for c in llm.paced(first_stuck, {"sent": clock.time()})]
        self.assertEqual(texts, ["a", "b"])  # the second request answered
        self.assertLess(clock.time() - started, 1.2)
        self.assertEqual(len(calls), 2)

        def silent():
            clock.sleep(5)
            return (c for c in [])

        started = clock.time()
        with self.assertRaises(llm.FirstTokenTimeout):
            list(llm.paced(silent, {"sent": clock.time()}))
        self.assertLess(clock.time() - started, 2)

    def test_unused_stream_stops_while_chosen_stream_is_still_active(self):
        import threading
        import time as clock

        import llm

        calls, discarded = [], []
        release_loser, release_winner, loser_closed = (threading.Event() for _ in range(3))

        def open_stream():
            calls.append(1)
            if len(calls) == 1:

                def loser():
                    try:
                        release_loser.wait(3)
                        for _ in range(100):
                            discarded.append(1)
                            yield self.chunk("discarded")
                    finally:
                        loser_closed.set()

                return loser()

            def winner():
                yield self.chunk("used")
                release_winner.wait(3)

            return winner()

        stream = llm.paced(open_stream, {"sent": clock.time()})
        try:
            self.assertEqual(next(stream).choices[0].delta.content, "used")
            release_loser.set()
            self.assertTrue(loser_closed.wait(1))
            self.assertEqual(len(calls), 2)
            self.assertLessEqual(len(discarded), 1)
            release_winner.set()
            self.assertEqual(list(stream), [])
        finally:
            release_loser.set()
            release_winner.set()
            stream.close()


class StreamTest(unittest.TestCase):
    def test_stream_is_assembled_into_a_replayable_message(self):
        from types import SimpleNamespace as N

        import llm

        def chunk(delta=None, finish=None, usage=None):
            fields = {"content": None, "tool_calls": None, "model_extra": {}, **(delta or {})}
            choices = [N(delta=N(**fields), finish_reason=finish)] if delta is not None or finish else []
            return N(choices=choices, usage=usage)

        def call(i, id=None, name=None, args=None):
            return N(index=i, id=id, function=N(name=name, arguments=args))

        stream = [
            chunk({"reasoning_content": "think "}),
            chunk({"reasoning_content": "more"}),
            chunk({"content": "ok"}),
            chunk({"tool_calls": [call(0, "c1", "read", '{"pa')]}),
            chunk({"tool_calls": [call(0, args='th": 1}')]}),
            chunk({"tool_calls": [call(1, "c2", "bash", "{}")]}),
            chunk(finish="tool_calls"),
            chunk(usage=N(prompt_tokens=5, completion_tokens=3)),
        ]
        message, finish, usage = llm.LLM._receive(stream)
        self.assertEqual(message["reasoning_content"], "think more")
        self.assertEqual(message["content"], "ok")
        self.assertEqual([c["function"]["arguments"] for c in message["tool_calls"]], ['{"path": 1}', "{}"])
        self.assertEqual(message["tool_calls"][0]["id"], "c1")
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(usage.prompt_tokens, 5)


class BrokenStreamTest(unittest.TestCase):
    def test_a_cut_stream_is_resent_with_low_effort_and_is_not_an_outage(self):
        import json as js
        from types import SimpleNamespace as N

        import llm

        def chunk(text=None, finish=None, usage=None):
            delta = N(content=text, tool_calls=None, reasoning_content=None, model_extra={})
            return N(choices=[N(delta=delta, finish_reason=finish)] if text or finish else [], usage=usage)

        efforts, limits = [], []

        def create(**request):
            efforts.append(request["extra_body"]["reasoning_effort"])
            limits.append(request["max_tokens"])

            def stream():
                yield chunk("partial ")
                if len(efforts) < 3:
                    raise js.JSONDecodeError("Unterminated string", "{", 1)
                yield chunk("done", finish="stop")
                yield chunk(
                    usage=N(
                        prompt_tokens=5, completion_tokens=2, prompt_tokens_details=None, completion_tokens_details=None
                    )
                )

            return stream()

        saved = llm.RETRY_SECONDS
        llm.RETRY_SECONDS = 1  # a cut stream that just delivered data must not count as an outage
        try:
            client = llm.LLM("deepseek-v4-flash-vision-exp", api_key="x", base_url="http://127.0.0.1:9")
            client.client = N(chat=N(completions=N(create=create)))
            reply = client.chat("sys", [{"role": "user", "content": "hi"}], [])
        finally:
            llm.RETRY_SECONDS = saved
        self.assertEqual(reply.text, "partial done")
        self.assertEqual(efforts, ["high", "high", "low"])
        self.assertEqual(limits[1:], [4096, 4096])  # below the cut, never under 4096 tokens


class ModelLimitsTest(unittest.TestCase):
    def test_window_and_output_limit_follow_the_effective_model(self):
        import os
        import subprocess

        code = (
            "import compaction, llm; print(llm.DEFAULT_MODEL, llm.SLOW_START, llm.MAX_TOKENS, "
            "compaction.TRIGGER_TOKENS)"
        )

        def limits(model: str) -> list[str]:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MODEL", "AGENT_"))}
            env.update({"MODEL": model} if model else {})
            return subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parent.parent / "src",
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()

        self.assertEqual(limits(""), ["deepseek-v4-flash-vision-exp", "False", "131072", "400000"])
        self.assertEqual(limits("kimi-k2"), ["kimi-k2", "False", "65536", "150000"])
        self.assertEqual(limits("deepseek-v4-flash"), ["deepseek-v4-flash", "False", "131072", "400000"])


if __name__ == "__main__":
    unittest.main()
