"""LLM adapters with tool use. Each keeps its own message history in its provider's format.

- AnthropicProvider: Claude via the Anthropic Messages API (needs ANTHROPIC_API_KEY)
- OpenAIProvider:    OpenAI Chat Completions, or any compatible server: Groq (free tier, no credit
                     card) or Ollama (free, local; base_url http://localhost:11434/v1, no key)
- ScriptedProvider:  a fixed sequence of tool calls, for tests and CI (no network)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


class AnthropicProvider:
    def __init__(self, model: str = "claude-sonnet-5-5", max_tokens: int = 4096):
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("Set ANTHROPIC_API_KEY, or use --provider ollama for a free local model")
        self.client, self.model, self.max_tokens = anthropic.Anthropic(), model, max_tokens
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict], goal: str) -> Turn:
        self.system, self.tools = system, tools
        self.messages = [{"role": "user", "content": goal}]
        return self._send()

    def reply(self, results: list[tuple[ToolCall, str, bool]]) -> Turn:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": text, "is_error": err} for c, text, err in results]})
        return self._send()

    def remind(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._send()

    def _send(self) -> Turn:
        r = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=self.system,
                                        tools=self.tools, messages=self.messages)
        self.messages.append({"role": "assistant", "content": [b.model_dump() for b in r.content]})
        turn = Turn()
        for b in r.content:
            if b.type == "text":
                turn.text += b.text
            elif b.type == "tool_use":
                turn.calls.append(ToolCall(b.id, b.name, dict(b.input)))
        return turn


class OpenAIProvider:
    def __init__(self, model: str = "gpt-4.1-mini", base_url: str | None = None, api_key: str | None = None,
                 max_retries: int = 2):
        import openai

        key = api_key or os.environ.get("OPENAI_API_KEY") or ("ollama" if base_url else None)
        if not key:
            raise RuntimeError("Set OPENAI_API_KEY, or pass --base-url for a local server such as Ollama")
        # max_retries: the SDK waits and retries on rate limits (HTTP 429), which free tiers hit often
        self.client = openai.OpenAI(base_url=base_url, api_key=key, max_retries=max_retries)
        self.model = model
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict], goal: str) -> Turn:
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools]
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": goal}]
        return self._send()

    def reply(self, results: list[tuple[ToolCall, str, bool]]) -> Turn:
        for c, text, _ in results:
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
        return self._send()

    def remind(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._send()

    def _send(self) -> Turn:
        r = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools)
        msg = r.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        self.messages.append({"role": "assistant", "content": msg.content or "",
                              **({"tool_calls": [tc.model_dump() for tc in msg.tool_calls]} if msg.tool_calls else {})})
        return Turn(msg.content or "", calls)


class ScriptedProvider:
    """Replays a fixed plan: each step is a list of (tool name, args). Records what it was sent."""

    def __init__(self, steps: list[list[tuple[str, dict]]], final_text: str = "Done."):
        self.steps, self.final_text, self.received = list(steps), final_text, []
        self.i = 0

    def start(self, system: str, tools: list[dict], goal: str) -> Turn:
        self.tools = tools
        return self._next()

    def reply(self, results) -> Turn:
        self.received.append([(c.name, json.loads(text), err) for c, text, err in results])
        return self._next()

    def remind(self, text: str) -> Turn:
        self.reminders = getattr(self, "reminders", []) + [text]
        return self._next()

    def _next(self) -> Turn:
        if self.i >= len(self.steps):
            return Turn(self.final_text)
        step = self.steps[self.i]
        self.i += 1
        return Turn("", [ToolCall(f"call_{self.i}_{j}", n, a) for j, (n, a) in enumerate(step)])


# Groq's model list changes over time; prefer large models with reliable tool calling
GROQ_PREFERENCE = ("gpt-oss-120b", "qwen3", "llama-4-maverick", "llama-3.3-70b", "kimi-k2", "gpt-oss-20b",
                   "llama-4-scout")


def pick_model(available: list[str]) -> str:
    """First available model matching the preference list (substring match, so 'openai/gpt-oss-120b' fits)."""
    chat = [m for m in available if not any(x in m for x in ("whisper", "tts", "guard", "embed", "playai"))]
    for pref in GROQ_PREFERENCE:
        hits = [m for m in chat if pref in m]
        if hits:
            return sorted(hits, key=len)[0]
    if chat:
        return chat[0]
    raise RuntimeError(f"No chat model found among: {', '.join(available)}")


def make_provider(name: str, model: str | None = None, base_url: str | None = None):
    if name == "anthropic":
        return AnthropicProvider(model or "claude-sonnet-5-5")
    if name == "openai":
        return OpenAIProvider(model or "gpt-4.1-mini", base_url)
    if name == "ollama":
        return OpenAIProvider(model or "qwen2.5:7b", base_url or "http://localhost:11434/v1")
    if name == "groq":   # free tier, no credit card: console.groq.com
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("Set GROQ_API_KEY (free key, no credit card, from console.groq.com)")
        prov = OpenAIProvider("pending", base_url or "https://api.groq.com/openai/v1", api_key=key, max_retries=8)
        available = sorted(m.id for m in prov.client.models.list().data)
        if model and model not in available:
            raise RuntimeError(f"Groq has no model {model!r}. Available: {', '.join(available)}")
        prov.model = model or pick_model(available)
        print(f"Using Groq model {prov.model}", flush=True)
        return prov
    raise ValueError(f"Unknown provider {name!r}; use anthropic, openai, groq or ollama")
