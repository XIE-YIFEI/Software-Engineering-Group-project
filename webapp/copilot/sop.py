"""The SOP file (webapp/sop.yaml): next steps per alert code and role, in English and Chinese.

The copilot only quotes these steps. They are reloaded when the file changes, and `validate()` (run by the tests)
checks that every list has both languages with the same number of steps.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

PATH = Path(__file__).resolve().parent.parent / "sop.yaml"
LANGS = ("en", "zh")
_cache: Dict[str, Any] = {"mtime": None, "data": None}


def load() -> Dict[str, Any]:
    mtime = PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        with open(PATH, encoding="utf-8") as f:
            _cache.update(mtime=mtime, data=yaml.safe_load(f) or {})
    return _cache["data"]


def validate(data: Dict[str, Any] = None) -> List[str]:
    """Problems of the SOP file (empty = fine)."""
    data = data if data is not None else load()
    problems = []
    if data.get("status") not in ("draft", "reviewed"):
        problems.append("status must be draft or reviewed")
    if data.get("status") == "reviewed" and not (data.get("reviewed_by") and data.get("reviewed_on")):
        problems.append("a reviewed SOP needs reviewed_by and reviewed_on")
    entries = [(f"sops.{code}.{role}", steps) for code, roles in (data.get("sops") or {}).items()
               for role, steps in (roles or {}).items()]
    entries += [(f"defaults.{role}", steps) for role, steps in (data.get("defaults") or {}).items()]
    for where, steps in entries:
        if not isinstance(steps, dict) or any(not isinstance(steps.get(lang), list) or not steps[lang] for lang in LANGS):
            problems.append(f"{where}: needs non-empty en and zh lists")
        elif len(steps["en"]) != len(steps["zh"]):
            problems.append(f"{where}: en has {len(steps['en'])} steps, zh {len(steps['zh'])}")
    return problems


def steps(code: str, role: str) -> Dict[str, Any]:
    """{found, en: [...], zh: [...], status, reviewed_by, reviewed_on, source} for one alert code and role."""
    data = load()
    entry = ((data.get("sops") or {}).get(code) or {}).get(role)
    scope = "alert code"
    if entry is None:
        entry, scope = (data.get("defaults") or {}).get(role), "role default"
    out = {"found": entry is not None, "scope": scope if entry is not None else None,
           "status": data.get("status", "draft"), "reviewed_by": data.get("reviewed_by") or "",
           "reviewed_on": str(data.get("reviewed_on") or ""), "source": "webapp/sop.yaml"}
    for lang in LANGS:
        out[lang] = list((entry or {}).get(lang) or [])
    return out
