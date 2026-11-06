"""The copilot's chat loop: one question -> a stream of events for the panel.

    meta        the conversation id, the model, a notice when a new conversation was started
    text        a piece of the answer as the model writes it
    step        a tool call starts (name and input) / step_done: it ended (a one-line summary)
    cite        the records the tools returned so far (ref -> chip data: id, plate masked per role, time, video)
    card        an action card was prepared (nothing written yet)
    offline     the AI service is not available: the reason and the quick questions (templates)
    error       the question could not be answered
    done        the stored answer: text, tool steps, citations, cards, SOP panels, the number check, cost

Each model call is one `llm.stream`; the tools run between calls with the signed-in user's permissions (as
via="assistant"). The transcript is stored, append-only, only when the loop ended cleanly, so it stays a valid request
for the next question; tool calls are in the audit log either way.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import clock, db
from ..privacy import audit
from ..security import Forbidden, User
from . import config, numbers, offline, prompts, store, tools
from .llm import AnthropicLLM, LLMError, LLMInvalidToolJSON, LLMUnavailable

Event = Tuple[str, Any]
MAX_QUESTION = 2000
KNOWN = "PDPA 2010 · Road Transport Act 1987 (Act 333)"       # names the prompt itself gives the model
REF = re.compile(r"\[\[([A-Z]:\d+)\]\]")


def prompt_hash(system: List[Dict[str, Any]], tool_defs: List[Dict[str, Any]], model: str) -> str:
    return hashlib.sha256(json.dumps([model, system, tool_defs], sort_keys=True).encode("utf-8")).hexdigest()


def make_llm(cfg: config.CopilotConfig):
    return AnthropicLLM(cfg.model, cfg.effort)


def _earlier_citations(conn: sqlite3.Connection, conv_id: int) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in db.rows(conn, "SELECT display FROM copilot_messages WHERE conversation_id = ? AND role = 'assistant' AND "
                           "display IS NOT NULL ORDER BY seq", (conv_id,)):
        out.update(json.loads(r["display"]).get("citations") or {})
    return out


def ask(conn: sqlite3.Connection, user: User, question: str, conversation_id: Optional[int] = None,
        page: Optional[Dict[str, Any]] = None, llm: Any = None) -> Iterator[Event]:
    question = (question or "").strip()
    if not question:
        yield "error", {"message": "Type a question."}
        return
    if len(question) > MAX_QUESTION:
        yield "error", {"message": f"Questions are limited to {MAX_QUESTION} characters."}
        return
    cfg = config.load(conn)
    if llm is None:
        st = config.status(conn, cfg)
        if not st["online"]:
            yield "offline", {"reason": st["reason"], "quick": offline.quick_questions(user.role)}
            return
        llm = make_llm(cfg)
    try:
        store.check_rate(conn, user, cfg.questions_per_hour)
    except Forbidden as e:
        yield "error", {"message": str(e)}
        return
    system = prompts.system_blocks(user.role)
    tool_defs = tools.definitions(user.role)
    phash = prompt_hash(system, tool_defs, llm.model)
    notice = None
    conv = None
    if conversation_id:
        conv = store.conversation(conn, user, conversation_id)
        if conv["prompt_hash"] != phash:
            notice, conv = "The assistant was updated (model, prompt or tools), so this question starts a new conversation.", None
        elif store.questions_in(conn, conv["id"]) >= config.MAX_QUESTIONS:
            notice, conv = f"A conversation holds {config.MAX_QUESTIONS} questions; this one starts a new conversation.", None
    created = conv is None
    conv_id = conv["id"] if conv else store.create_conversation(conn, user, question[:60], llm.model, phash)
    yield "meta", {"conversation_id": conv_id, "model": llm.model, "notice": notice, "new": created}

    user_msg = {"role": "user", "content": [{"type": "text", "text": question}]}
    ctx_msg = prompts.context_message(conn, user, page)
    history = store.transcript(conn, conv_id)
    messages = history + [user_msg, ctx_msg]
    new: List[Dict[str, Any]] = [user_msg, ctx_msg]
    via = replace(user, via="assistant")
    ctx = tools.Ctx(conn, via, conv_id)
    steps: List[Dict[str, Any]] = []
    payloads: List[Any] = []
    parts: List[str] = []
    usage_total: Dict[str, int] = {}
    cost = 0.0
    qkey: Optional[int] = None
    turn = None
    clean, problem = False, None
    n_cards = 0
    try:
        for _ in range(cfg.max_steps):
            for attempt in range(3):
                try:
                    turn = None
                    for kind, val in llm.stream(system=system, tools=tool_defs, messages=messages,
                                                max_tokens=cfg.max_tokens):
                        if kind == "text":
                            parts.append(val)
                            yield "text", val
                        elif kind == "turn":
                            turn = val
                    break
                except LLMInvalidToolJSON:
                    if attempt == 2:
                        raise
            rid = store.log_usage(conn, user, "chat", turn.model, turn.usage, turn.cost_usd, turn.stop_reason,
                                  turn.request_id, conv_id, qkey)
            qkey = qkey if qkey is not None else -rid
            cost += turn.cost_usd
            for k, v in turn.usage.items():
                usage_total[k] = usage_total.get(k, 0) + v
            if turn.stop_reason == "refusal":
                cat = (turn.stop_details or {}).get("category")
                problem = "The model declined this request" + (f" (category: {cat})" if cat else "") + "."
                break
            uses = turn.tool_uses
            if turn.stop_reason == "max_tokens":
                problem = "The answer was cut off at the length limit; ask a narrower question."
                break
            messages.append({"role": "assistant", "content": turn.content})
            new.append(messages[-1])
            if turn.stop_reason == "pause_turn":
                continue
            if not uses:
                clean = True
                break
            results = []
            for tu in uses:
                yield "step", {"id": tu["id"], "tool": tu["name"], "input": tu.get("input")}
                r = tools.execute(ctx, tu["name"], tu.get("input"))
                payloads.append(r.payload)
                steps.append({"tool": tu["name"], "input": tu.get("input"), "summary": r.summary(), "error": r.is_error})
                yield "step_done", {"id": tu["id"], "tool": tu["name"], "summary": r.summary(), "error": r.is_error}
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": r.text,
                                **({"is_error": True} if r.is_error else {})})
            for c in ctx.cards[n_cards:]:
                yield "card", c
            n_cards = len(ctx.cards)
            if ctx.citations:
                yield "cite", ctx.citations
            messages.append({"role": "user", "content": results})
            new.append(messages[-1])
            parts.append("\n\n")
            yield "text", "\n\n"
        else:
            problem = f"The question needed more than {cfg.max_steps} steps; ask a narrower question."
    except LLMUnavailable as e:
        problem = f"The AI service is not available: {e}. The quick questions (templates) still work."
    except LLMError as e:
        problem = f"The AI service could not answer: {e}."
    answer = "".join(parts).strip()
    cited = list(dict.fromkeys(REF.findall(answer)))
    known = {**_earlier_citations(conn, conv_id), **ctx.citations}
    check = numbers.check(answer, payloads + [ctx_msg["content"], KNOWN], question) if answer else \
        {"ok": True, "checked": 0, "unmatched": [], "plates_unmatched": []}
    check["unknown_refs"] = [r for r in cited if r not in known]
    check["ok"] = check["ok"] and not check["unknown_refs"]
    display = {"text": answer, "steps": steps, "citations": {r: known[r] for r in cited if r in known},
               "records": [r for r in ctx.citations if r not in cited][:60],
               "record_data": {r: ctx.citations[r] for r in list(ctx.citations)[:60]},
               "cards": [c["id"] for c in ctx.cards], "panels": ctx.panels, "check": check,
               "model": turn.model if turn else llm.model, "fallback": turn.fallback if turn else None,
               "usage": usage_total, "cost_usd": round(cost, 6), "ai": True, "problem": problem, "at": clock.now_iso()}
    message_id = None
    if clean:
        ids = store.append(conn, conv_id, new, {0: {"text": question, "page": page or {}, "at": clock.now_iso()},
                                                len(new) - 1: display})
        message_id = ids[-1]
        if qkey is not None:
            store.link_question(conn, qkey, message_id, conv_id)
    elif created:
        conn.execute("DELETE FROM copilot_conversations WHERE id = ?", (conv_id,))
        conv_id = None
    audit(conn, via, "copilot_question", "copilot_conversation", conv_id,
          {"model": display["model"], "tools": [s["tool"] for s in steps], "cards": display["cards"],
           "cost_usd": display["cost_usd"], "number_check": "ok" if check["ok"] else
           f"flagged {len(check['unmatched']) + len(check['plates_unmatched']) + len(check['unknown_refs'])}",
           "stored": clean, "problem": bool(problem)})
    if problem and not answer:
        yield "error", {"message": problem, "conversation_id": conv_id}
    yield "done", {"conversation_id": conv_id, "message_id": message_id, "display": display}


def collect(events: Iterator[Event]) -> Dict[str, Any]:
    """Run a question to the end (tests, the evaluation): {events, text, done, cards, offline, error}."""
    out: Dict[str, Any] = {"events": [], "text": "", "cards": [], "done": None, "offline": None, "error": None}
    for kind, val in events:
        out["events"].append((kind, val))
        if kind == "text":
            out["text"] += val
        elif kind == "card":
            out["cards"].append(val)
        elif kind in ("done", "offline", "error", "meta"):
            out[kind] = val
    return out
