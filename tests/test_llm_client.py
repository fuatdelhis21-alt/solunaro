"""Request shape for current Claude models, refusal handling and per-kind news requirement."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import llm as L  # noqa: E402
from desk.engine import min_news_for  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self._d, self.text = status, data, str(data)

    def json(self):
        return self._d


def ok(text='{"decision":"WAIT"}', stop="end_turn", model="claude-opus-5-5"):
    return Resp({"model": model, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5},
                 "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}]})


@mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
class TestLLMClient(unittest.TestCase):
    def call(self, resp, **kw):
        c = L.AnthropicLLM("claude-opus-5-5", **kw)
        with mock.patch.object(L.requests, "post", return_value=resp) as post:
            out = c.ask_json("sys", "user", agent=kw.pop("agent", "NOVA"))
        return c, post, out

    def test_body_has_effort_no_temperature_and_fallback(self):
        c, post, out = self.call(ok())
        body, headers = post.call_args.kwargs["json"], post.call_args.kwargs["headers"]
        self.assertNotIn("temperature", body)
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(headers["anthropic-beta"], "server-side-fallback-2026-07-01")
        self.assertEqual(out, {"decision": "WAIT"})       # thinking block ignored

    def test_haiku_gets_no_fallbacks(self):
        c = L.AnthropicLLM("claude-opus-5-5", models={"ATLAS": "claude-haiku-5-5"})
        with mock.patch.object(L.requests, "post", return_value=ok(model="claude-haiku-5-5")) as post:
            c.ask_json("s", "u", agent="ATLAS")
        self.assertNotIn("fallbacks", post.call_args.kwargs["json"])
        self.assertNotIn("anthropic-beta", post.call_args.kwargs["headers"])

    def test_served_model_is_logged(self):
        c, _, _ = self.call(ok(model="claude-opus-5"))
        self.assertEqual(c.drain_calls()[0]["model"], "claude-opus-5")

    def test_refusal_and_truncation_raise_once(self):
        for stop in ("refusal", "max_tokens"):
            c = L.AnthropicLLM("claude-opus-5-5")
            with mock.patch.object(L.requests, "post", return_value=ok(stop=stop)) as post, \
                    mock.patch.object(L.time, "sleep"):
                with self.assertRaises(L.LLMRefusal):
                    c.ask_json("s", "u", agent="NOVA")
            self.assertEqual(post.call_count, 1)


class TestMinNews(unittest.TestCase):
    def test_per_kind_and_scalar(self):
        self.assertEqual(min_news_for({"min_news_items": {"crypto": 1, "dex": 0}}, "dex"), 0)
        self.assertEqual(min_news_for({"min_news_items": {"crypto": 1, "dex": 0}}, "crypto"), 1)
        self.assertEqual(min_news_for({"min_news_items": 2}, "dex"), 2)
        self.assertEqual(min_news_for({}, "crypto"), 0)


if __name__ == "__main__":
    unittest.main()
