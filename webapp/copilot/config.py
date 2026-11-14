"""Copilot configuration: model, effort, limits, prices, and whether the AI service is available.

The API key is never stored by the system: the Anthropic SDK reads ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN, or an
`ant auth login` profile) from the environment of the server process. Environment variables set the defaults; the
administrator can switch the model, the effort and the optional image checks on the Settings page (stored in the
settings table, audit-logged):

    SECS_COPILOT_MODEL      claude-opus-5-5 (default) | claude-sonnet-5-5 | claude-haiku-5-5
    SECS_COPILOT_EFFORT     low | medium (default) | high
    SECS_COPILOT_OFFLINE=1  force offline mode: template answers, no model calls (e.g. a demo without network)

The rate limit, the daily budget and the retention of conversations are ordinary admin settings (webapp/settings.py).
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .. import clock, db

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (Claude API list prices, October 2026): input, output, 5-minute cache write, cache read.
# Update when the prices change. `fallbacks`: the model accepts server-side refusal fallbacks (fallbacks: "default").
MODELS: Dict[str, Dict[str, Any]] = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5", "price": (4.00, 20.00, 5.00, 0.20), "fallbacks": True},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5", "price": (2.00, 10.00, 2.50, 0.20), "fallbacks": True},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5", "price": (0.10, 0.50, 0.125, 0.01), "fallbacks": False},
}
# Models a refused request may be re-run on by the server-side fallback; they bill at their own prices.
OTHER_PRICES: Dict[str, Tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50), "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20)}
EFFORTS = ("low", "medium", "high")

MAX_TOKENS = 16000          # per model call; adaptive thinking counts towards it
MAX_STEPS = 8               # model calls per question (each tool round is one)
MAX_QUESTIONS = 12          # per conversation; then the panel starts a new one (keeps the context and cost small)
ACTION_MINUTES = 30         # a prepared action card expires after this
UNREACHABLE_PAUSE = 120     # seconds offline after the model service could not be reached

# Autumn 2026 (development plan Rev2): the copilot is READ-ONLY. The four action tools (visitor pass, list entry,
# alert status, full-plate reveal) are not offered to the model, a call to one is refused (tools.execute), and
# actions.py refuses to prepare or confirm a card even when the user confirms one. The ordinary pages keep these
# operations for the roles allowed them. A code constant, not a setting: turning the actions back on is a separate,
# reviewed change (the prompt, the panel texts and the docs say read-only too). The tests switch it on only to keep
# the dormant card code working.
ACTIONS_ENABLED = False
READ_ONLY = ("the copilot is read-only: it cannot issue passes, change lists or alert statuses, or reveal plates, and "
             "it does not prepare these for confirmation. Do it on the page itself, with your role's own permissions.")


def price(model: str) -> Tuple[float, float, float, float]:
    if model in MODELS:
        return MODELS[model]["price"]
    for name, p in OTHER_PRICES.items():
        if model.startswith(name):
            return p
    return MODELS[DEFAULT_MODEL]["price"]                     # unknown: count it at the default model's prices


def cost_usd(model: str, usage: Dict[str, int]) -> float:
    p_in, p_out, p_write, p_read = price(model)
    return round((usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out
                  + usage.get("cache_creation_input_tokens", 0) * p_write
                  + usage.get("cache_read_input_tokens", 0) * p_read) / 1e6, 6)


@dataclass
class CopilotConfig:
    model: str
    effort: str
    vlm_enabled: bool
    questions_per_hour: int
    daily_budget_usd: float
    retention_days: int
    max_tokens: int = MAX_TOKENS
    max_steps: int = MAX_STEPS

    @property
    def model_label(self) -> str:
        return MODELS.get(self.model, {}).get("label", self.model)


def load(conn: sqlite3.Connection) -> CopilotConfig:
    model = db.get_setting(conn, "copilot_model") or os.environ.get("SECS_COPILOT_MODEL") or DEFAULT_MODEL
    effort = db.get_setting(conn, "copilot_effort") or os.environ.get("SECS_COPILOT_EFFORT") or "medium"
    return CopilotConfig(model=model if model in MODELS else DEFAULT_MODEL,
                         effort=effort if effort in EFFORTS else "medium",
                         vlm_enabled=bool(db.get_setting(conn, "copilot_vlm_enabled") or False),
                         questions_per_hour=int(db.get_setting(conn, "copilot_questions_per_hour")),
                         daily_budget_usd=float(db.get_setting(conn, "copilot_daily_budget_usd")),
                         retention_days=int(db.get_setting(conn, "copilot_retention_days")))


# ---------------------------------------------------------------------------- availability
_state: Dict[str, Any] = {"checked": 0.0, "creds": None, "down_until": 0.0, "down_reason": ""}


def credentials() -> Tuple[bool, str]:
    """Whether the SDK finds a credential (no network call; cached for a minute)."""
    if time.monotonic() - _state["checked"] < 60 and _state["creds"] is not None:
        return _state["creds"]
    try:
        import anthropic
    except ImportError:
        found = (False, "the anthropic package is not installed (pip install -r requirements.txt)")
    else:
        try:
            c = anthropic.Anthropic()
            ok = bool(c.api_key or c.auth_token or getattr(c, "credentials", None) is not None)
            found = (ok, "" if ok else "no API key: set ANTHROPIC_API_KEY (or run `ant auth login`) and restart")
        except Exception as e:                                          # noqa: BLE001 - a bad profile file etc.
            found = (False, f"the Anthropic client could not start: {e}")
    _state.update(checked=time.monotonic(), creds=found)
    return found


def mark_unreachable(reason: str, seconds: float = UNREACHABLE_PAUSE) -> None:
    """After a connection or authentication failure: answer offline for a while instead of failing every request."""
    _state.update(down_until=time.monotonic() + seconds, down_reason=reason)


def reset_availability() -> None:
    _state.update(checked=0.0, creds=None, down_until=0.0, down_reason="")


def spent_today(conn: sqlite3.Connection) -> float:
    return float(db.scalar(conn, "SELECT TOTAL(cost_usd) FROM copilot_usage WHERE at >= ?",
                           (clock.now_iso()[:10],)) or 0.0)


def status(conn: sqlite3.Connection, cfg: Optional[CopilotConfig] = None) -> Dict[str, Any]:
    """{online, reason, model, ...}: online = a credential is found, not forced offline, not over budget, and the
    service was not unreachable in the last two minutes."""
    cfg = cfg or load(conn)
    spent = spent_today(conn)
    online, reason = credentials()
    if os.environ.get("SECS_COPILOT_OFFLINE", "").strip() not in ("", "0"):
        online, reason = False, "offline mode is forced (SECS_COPILOT_OFFLINE)"
    elif online and spent >= cfg.daily_budget_usd:
        online, reason = False, f"today's budget of {cfg.daily_budget_usd:.2f} USD is used up"
    elif online and time.monotonic() < _state["down_until"]:
        online, reason = False, _state["down_reason"]
    return {"online": online, "reason": reason, "model": cfg.model, "model_label": cfg.model_label,
            "effort": cfg.effort, "vlm_enabled": cfg.vlm_enabled, "spent_today_usd": round(spent, 4),
            "daily_budget_usd": cfg.daily_budget_usd, "questions_per_hour": cfg.questions_per_hour,
            "retention_days": cfg.retention_days, "fallbacks": bool(MODELS.get(cfg.model, {}).get("fallbacks")),
            "actions_enabled": ACTIONS_ENABLED}
