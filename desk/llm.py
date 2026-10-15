"""Minimal Anthropic Messages API client (requests only) returning parsed JSON."""
from __future__ import annotations

import json
import os
import re
import time

import requests

API_URL = "https://api.anthropic.com/v1/messages"


class LLMError(Exception):
    pass


class LLMRefusal(LLMError):
    """Model declined or ran out of tokens. The cycle is journaled as ERROR; no trade."""


def extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"model did not return valid JSON: {e}; head={text[:200]!r}") from e


class AnthropicLLM:
    def __init__(self, model: str, max_tokens: int = 16000, effort: str | None = "medium",
                 timeout: int = 180, models: dict | None = None, fallbacks: bool = True):
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.model, self.max_tokens = model, max_tokens
        self.effort, self.timeout, self.fallbacks = effort, timeout, fallbacks
        self.models = models or {}          # per-agent override, e.g. {"ATLAS": "<cheap model>"}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        self.calls: list[dict] = []          # drained by the engine into sqlite after each cycle

    def drain_calls(self) -> list[dict]:
        out, self.calls = self.calls, []
        return out

    def ask_json(self, system: str, user: str, agent: str | None = None) -> dict:
        model = self.models.get(agent) or self.model
        # Current models think adaptively by default and reject non-default temperature,
        # so depth is steered with effort only. Thinking tokens count toward max_tokens.
        body = {"model": model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        # Server-side refusal fallback exists for Opus/Sonnet/Fable, not for Haiku.
        if self.fallbacks and not model.startswith("claude-haiku"):
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        last = None
        for attempt in range(3):
            try:
                t0 = time.monotonic()
                r = requests.post(API_URL, json=body, headers=headers, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                if r.status_code in (429, 500, 502, 503, 529):
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                d = r.json()
                u = d.get("usage", {})
                self.usage["input_tokens"] += u.get("input_tokens", 0)
                self.usage["output_tokens"] += u.get("output_tokens", 0)
                self.usage["calls"] += 1
                served = d.get("model") or model         # differs from `model` after a fallback
                self.calls.append({"ts": int(time.time()), "agent": agent, "model": served,
                                   "input_tokens": u.get("input_tokens", 0),
                                   "output_tokens": u.get("output_tokens", 0),
                                   "latency_ms": latency_ms, "stop_reason": d.get("stop_reason")})
                stop = d.get("stop_reason")
                if stop == "refusal":
                    cat = (d.get("stop_details") or {}).get("category")
                    raise LLMRefusal(f"{agent or model} refused (category={cat})")
                if stop == "max_tokens":
                    raise LLMRefusal(f"{agent or model} hit max_tokens={self.max_tokens}")
                text = "".join(b.get("text", "") for b in d.get("content", [])
                               if b.get("type") == "text")
                return extract_json(text)
            except LLMRefusal:
                raise                                    # deterministic: retrying would not help
            except (requests.RequestException, LLMError) as e:
                last = e
                if isinstance(e, LLMError) and "HTTP 4" in str(e):
                    raise
                time.sleep(3 * (attempt + 1))
        raise LLMError(f"LLM failed after retries: {last}")
