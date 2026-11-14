"""The model behind the copilot: the official Anthropic Python SDK, or a scripted fake for tests and CI.

`stream(...)` is a generator: it yields ("text", delta) while the model writes and ends with ("turn", ModelTurn), one
model call per call of `stream`. The agent loop (agent.py) runs the tools between calls (a manual tool loop, so CI
can replace the model with `FakeLLM` and the browser can be sent every text delta and tool step as it happens).

Request settings (AnthropicLLM): adaptive thinking with an explicit effort; prompt caching with one breakpoint on the
frozen system prompt (which caches the tools before it as well) plus automatic caching of the growing conversation;
tool inputs streamed eagerly (validated against the tool's schema before anything runs, tools.py); and, on the models
that support it, the server-side fallback that re-runs a request a safety classifier declined on the model Anthropic
recommends for that case (`fallbacks: "default"`).
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from . import config

Event = Tuple[str, Any]


class LLMError(Exception):
    """The model could not answer (reason is shown to the user)."""


class LLMUnavailable(LLMError):
    """No connection, a rejected key, rate-limited or overloaded: answer offline instead."""


class LLMInvalidToolJSON(LLMError):
    """The model wrote tool input the SDK could not parse at all (re-issue the turn)."""


@dataclass
class ModelTurn:
    content: List[Dict[str, Any]]            # content blocks as plain dicts, replayed verbatim on the next call
    stop_reason: Optional[str]
    model: str                               # the model that served it (a fallback model after a refusal)
    usage: Dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    stop_details: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None
    fallback: Optional[str] = None           # "claude-opus-5-5 -> claude-opus-5" when a fallback model answered

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> List[Dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def clean_blocks(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Content blocks as they may be sent back: no None fields, no SDK-only fields; after a fallback, the
    thinking / tool_use blocks before the last switch point are dropped (only the fallback model's turn counts)."""
    out = [{k: v for k, v in b.items() if v is not None and k != "parsed_output"} for b in blocks]
    cut = max((i for i, b in enumerate(out) if b.get("type") == "fallback"), default=None)
    if cut is not None:
        out = [b for i, b in enumerate(out) if b.get("type") != "fallback" and not (
            i < cut and b.get("type") in ("thinking", "redacted_thinking", "tool_use", "server_tool_use"))]
    return out


def usage_dict(u: Any) -> Dict[str, int]:
    if u is None:
        return {}
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    return {k: int(get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                          "cache_read_input_tokens")}


def turn_cost(model: str, usage: Any) -> float:
    """Cost of one call from `usage`. With a fallback, usage.iterations lists every attempt (each billed at the price
    of the model that ran it); otherwise the top-level usage is the call."""
    iterations = (usage.get("iterations") if isinstance(usage, dict) else getattr(usage, "iterations", None)) or []
    total = 0.0
    counted = False
    for it in iterations:
        u = usage_dict(it)
        if any(u.values()):
            m = (it.get("model") if isinstance(it, dict) else getattr(it, "model", None)) or model
            total += config.cost_usd(m, u)
            counted = True
    return round(total, 6) if counted else config.cost_usd(model, usage_dict(usage))


class AnthropicLLM:
    """Claude through the official SDK (anthropic >= 1.17, the beta messages surface for the fallback parameter)."""
    no_fallbacks = False                    # set when the account rejects the server-side fallback beta

    def __init__(self, model: str = config.DEFAULT_MODEL, effort: str = "medium", client: Any = None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(max_retries=2, timeout=120.0)
        self.client = client
        self.model = model
        self.effort = effort

    def stream(self, *, system: List[Dict[str, Any]], messages: List[Dict[str, Any]],
               tools: Optional[List[Dict[str, Any]]] = None, max_tokens: int = config.MAX_TOKENS,
               output_schema: Optional[Dict[str, Any]] = None) -> Iterator[Event]:
        import anthropic
        output_config: Dict[str, Any] = {"effort": self.effort}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        kw: Dict[str, Any] = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                                  thinking={"type": "adaptive"}, output_config=output_config,
                                  cache_control={"type": "ephemeral"})
        if tools:
            kw["tools"] = [{**t, "eager_input_streaming": True} for t in tools]
        if config.MODELS.get(self.model, {}).get("fallbacks") and not AnthropicLLM.no_fallbacks:
            kw["betas"] = ["server-side-fallback-2026-07-01"]
            kw["fallbacks"] = "default"
        try:
            try:
                with self.client.beta.messages.stream(**kw) as s:
                    for event in s:
                        if event.type == "text":
                            yield "text", event.text
                    msg = s.get_final_message()
                    request_id = getattr(s, "request_id", None)
            except anthropic.BadRequestError as e:
                if "fallbacks" not in kw or "fallback" not in str(e.message).lower():
                    raise
                AnthropicLLM.no_fallbacks = True    # this account does not take the beta: carry on without it
                kw.pop("fallbacks")
                kw.pop("betas")
                with self.client.beta.messages.stream(**kw) as s:
                    for event in s:
                        if event.type == "text":
                            yield "text", event.text
                    msg = s.get_final_message()
                    request_id = getattr(s, "request_id", None)
        except ValueError as e:                     # tool JSON the SDK could not parse at all (not an API error)
            raise LLMInvalidToolJSON(str(e)) from e
        except anthropic.AuthenticationError as e:
            config.mark_unreachable("the API key was rejected (check ANTHROPIC_API_KEY)", 600)
            raise LLMUnavailable("the API key was rejected") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMUnavailable(f"this key may not use {self.model}: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"model {self.model} not found: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMUnavailable("the model service is rate-limiting this key; try again in a minute") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"the model service rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMUnavailable(f"the model service is busy or failing (HTTP {e.status_code}); try again") from e
            raise LLMError(f"the model service returned HTTP {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            config.mark_unreachable("the model service could not be reached (no network?)")
            raise LLMUnavailable("the model service could not be reached") from e
        blocks = [b.to_dict() for b in msg.content]
        fb = next((b for b in reversed(blocks) if b.get("type") == "fallback"), None)
        details = msg.stop_details.to_dict() if getattr(msg, "stop_details", None) is not None else None
        yield "turn", ModelTurn(
            content=clean_blocks(blocks), stop_reason=msg.stop_reason, model=msg.model, usage=usage_dict(msg.usage),
            cost_usd=turn_cost(msg.model, msg.usage), stop_details=details, request_id=request_id,
            fallback=f"{fb['from']['model']} -> {fb['to']['model']}" if fb else None)


# ---------------------------------------------------------------------------- the fake model (tests, CI, dry runs)
Script = Union[Sequence[Sequence[Dict[str, Any]]], Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], List[Dict[str, Any]]]]
_ids = itertools.count(1)


def text(t: str) -> Dict[str, Any]:
    return {"type": "text", "text": t}


def tool(name: str, **inp: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_fake_{next(_ids):04d}", "name": name, "input": inp}


class FakeLLM:
    """A deterministic stand-in for Claude. `script` is either a list of turns (each a list of content blocks, used
    in order, one per model call) or a function (messages, tools) -> blocks. It streams the text in small pieces,
    reports plausible token counts and records every request (`calls`) for the tests."""

    def __init__(self, script: Script, model: str = "fake-model"):
        self.script = script
        self.model = model
        self.effort = "medium"
        self.calls: List[Dict[str, Any]] = []
        self._turns = iter(script) if not callable(script) else None

    def stream(self, *, system, messages, tools=None, max_tokens=config.MAX_TOKENS, output_schema=None):
        self.calls.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools or [],
                           "max_tokens": max_tokens, "output_schema": output_schema})
        if self._turns is not None:
            try:
                blocks = list(next(self._turns))
            except StopIteration:
                blocks = [text("(fake model: script exhausted)")]
        else:
            blocks = list(self.script(messages, tools or []))
        for b in blocks:
            if b["type"] == "text":
                for i in range(0, len(b["text"]), 24):
                    yield "text", b["text"][i:i + 24]
        size = len(json.dumps(system)) + len(json.dumps(messages)) + len(json.dumps(tools or []))
        usage = {"input_tokens": size // 4, "output_tokens": max(1, len(json.dumps(blocks)) // 4),
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        if any(b.get("type") == "refusal" for b in blocks):
            blocks, stop = [], "refusal"
        yield "turn", ModelTurn(content=blocks, stop_reason=stop, model=self.model, usage=usage, cost_usd=0.0,
                                stop_details={"type": "refusal", "category": "test"} if stop == "refusal" else None)


def run(gen: Iterator[Event]) -> Tuple[str, Optional[ModelTurn]]:
    """Drain a stream: (all text, the final turn)."""
    parts, turn = [], None
    for kind, val in gen:
        if kind == "text":
            parts.append(val)
        elif kind == "turn":
            turn = val
    return "".join(parts), turn
