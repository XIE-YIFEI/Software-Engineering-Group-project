"""API: SECS Copilot - chat (streamed as server-sent events), quick questions, action cards (confirm refused while the
copilot is read-only), "AI explain", generated documents, the optional image checks and the copilot's admin settings.
See docs/system/COPILOT_EN.md."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import db
from ..copilot import actions, agent, config, documents, offline, store, tools, vlm
from ..deps import current_user, get_conn
from ..privacy import audit
from ..security import Forbidden, User, require
from ..settings import active

router = APIRouter()


def _stream(make: "callable") -> StreamingResponse:
    """Server-sent events from a generator of (event, data), on the request's own database connection."""
    def gen() -> Iterator[str]:
        conn = db.connect(active().db_path)
        try:
            for kind, val in make(conn):
                yield f"event: {kind}\ndata: {json.dumps(val, ensure_ascii=False, default=str)}\n\n"
        except Forbidden as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e), 'status': 403})}\n\n"
        except LookupError as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e).strip(chr(39)) or 'not found', 'status': 404})}\n\n"
        except ValueError as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e), 'status': 422})}\n\n"
        except Exception as e:                                          # noqa: BLE001 - tell the panel, keep the server
            import traceback
            traceback.print_exc()
            yield f"event: error\ndata: {json.dumps({'message': f'internal error ({type(e).__name__})', 'status': 500})}\n\n"
        finally:
            conn.close()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/copilot/status", tags=["copilot"])
def api_status(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Whether the AI service is available (and why not), the model, the limits, this role's quick questions and tools."""
    st = config.status(conn)
    return {**st, "quick": offline.quick_questions(user.role), "tools": tools.tools_by_role()[user.role],
            "requests_last_hour": store.requests_last_hour(conn, user)}


class ChatIn(BaseModel):
    message: str = Field(max_length=4000)
    conversation_id: Optional[int] = None
    page: Dict[str, Any] = Field(default_factory=dict)


@router.post("/copilot/chat", tags=["copilot"])
def api_chat(body: ChatIn, user: User = Depends(current_user)):
    """Ask the assistant. Streams events: meta, text, step, step_done, cite, card, offline, error, done."""
    page = {k: v for k, v in (body.page or {}).items() if k in ("path", "alert_id", "sighting_id", "run_id", "day")}
    return _stream(lambda conn: agent.ask(conn, user, body.message, body.conversation_id, page))


class QuickIn(BaseModel):
    id: str
    lang: str = "en"


@router.post("/copilot/quick", tags=["copilot"])
def api_quick(body: QuickIn, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """A quick question answered from fixed tool queries and templates (works offline; labelled as a template)."""
    return offline.answer(conn, user, body.id, body.lang)


@router.get("/copilot/conversations", tags=["copilot"])
def api_conversations(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return store.conversations(conn, user)


@router.get("/copilot/conversations/{conv_id}", tags=["copilot"])
def api_conversation(conv_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    h = store.history(conn, user, conv_id)
    ids = [i for item in h["items"] for i in item.get("cards", [])]
    h["cards"] = {r["id"]: actions.card(r) for r in db.rows(
        conn, f"SELECT * FROM copilot_actions WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
    return h


@router.delete("/copilot/conversations/{conv_id}", tags=["copilot"])
def api_delete_conversation(conv_id: int, user: User = Depends(current_user),
                            conn: sqlite3.Connection = Depends(get_conn)):
    store.delete_conversation(conn, user, conv_id)
    audit(conn, user, "copilot_delete", "copilot_conversation", conv_id)
    return {"ok": True}


class ConfirmIn(BaseModel):
    fields: Dict[str, Any] = Field(default_factory=dict)


@router.post("/copilot/actions/{action_id}/confirm", tags=["copilot"])
def api_confirm(action_id: int, body: ConfirmIn, user: User = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Confirm a prepared action: it runs now, as this user, through the ordinary service (audit: via=assistant)."""
    return actions.confirm(conn, user, action_id, body.fields)


@router.post("/copilot/actions/{action_id}/cancel", tags=["copilot"])
def api_cancel(action_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return actions.cancel(conn, user, action_id)


class LangIn(BaseModel):
    lang: str = "en"


@router.post("/copilot/alerts/{alert_id}/explain", tags=["copilot"])
def api_explain(alert_id: int, body: LangIn, user: User = Depends(current_user)):
    """"AI explain": how the alert was computed (from the stored values), the SOP steps and the legal references.
    Streams: facts, text, done."""
    require(user, "police", "guard", "admin")
    return _stream(lambda conn: documents.explain_alert(conn, user, alert_id, body.lang))


@router.post("/copilot/alerts/{alert_id}/case-summary", tags=["copilot"])
def api_case_summary(alert_id: int, body: LangIn, user: User = Depends(current_user)):
    """An AI case summary for the alert's evidence pack (police). Streams: text, done."""
    require(user, "police")
    return _stream(lambda conn: documents.case_summary(conn, user, alert_id, body.lang))


@router.get("/copilot/alerts/{alert_id}/case-summary", tags=["copilot"])
def api_latest_case_summary(alert_id: int, user: User = Depends(current_user),
                            conn: sqlite3.Connection = Depends(get_conn)):
    require(user, "police")
    from ..services import alerts as alert_svc
    alert_svc.get_alert(conn, user, alert_id)
    return documents.latest(conn, "case_summary", alert_id)


class ReviewIn(BaseModel):
    date: Optional[str] = None
    congestion_day: Optional[str] = None
    lang: str = "en"


@router.post("/copilot/reviews/daily", tags=["copilot"])
def api_daily_review(body: ReviewIn, user: User = Depends(current_user)):
    """The daily traffic and congestion review (traffic managers; police and admins may read it). Streams: text, done."""
    require(user, "traffic", "police", "admin")
    return _stream(lambda conn: documents.daily_review(conn, user, body.date, body.congestion_day, body.lang))


@router.get("/copilot/documents/{doc_id}.pdf", tags=["copilot"])
def api_document_pdf(doc_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    d = db.row(conn, "SELECT * FROM copilot_documents WHERE id = ?", (doc_id,))
    if d is None or (d["kind"] == "case_summary" and user.role != "police") or (
            d["kind"] == "daily_review" and user.role not in ("traffic", "police", "admin")) or d["kind"] == "alert_explanation":
        raise LookupError("no such document")
    data = render_pdf(documents._display(d))
    audit(conn, user, "export", "copilot_document", doc_id, {"format": "pdf", "kind": d["kind"]})
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={d['kind']}_{doc_id}.pdf"})


def render_pdf(doc: Dict[str, Any]) -> bytes:
    from ..services.evidence import MARK, pdf_document
    title = {"case_summary": "Case summary", "daily_review": "Daily traffic and congestion review"}.get(doc["kind"], "Document")
    pdf, t = pdf_document(title, MARK)
    F, W = pdf.family_name, pdf.w - 20
    pdf.set_font(F, "B", 15)
    pdf.cell(W, 9, t(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(F, "", 9)
    pdf.set_text_color(160, 40, 30)
    pdf.multi_cell(W, 5, t(f"{doc['label']} · {doc['model']} · {doc['created_at'][:16].replace('T', ' ')} · number check: "
                           + ("all numbers found in the system's data" if doc["check"].get("ok") else
                              f"{len(doc['check'].get('unmatched', []))} not found: "
                              + ", ".join(u["text"] for u in doc["check"].get("unmatched", [])))),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 24)
    for line in doc["text"].splitlines():
        s = line.strip()
        if s.startswith("#"):
            pdf.set_font(F, "B", 12)
            pdf.ln(1.5)
            pdf.multi_cell(W, 6.5, t(s.lstrip("#").strip()), new_x="LMARGIN", new_y="NEXT")
        elif s:
            pdf.set_font(F, "", 10)
            pdf.multi_cell(W, 5.2, t(s.replace("**", "")), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


@router.get("/copilot/reviews/daily/latest", tags=["copilot"])
def api_latest_review(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    require(user, "traffic", "police", "admin")
    return documents.latest(conn, "daily_review")


# ---------------------------------------------------------------------------- optional image checks (VLM)
@router.post("/copilot/vlm/second-reading/{sighting_id}", tags=["copilot"])
def api_second_reading(sighting_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Police, image checks switched on: the model reads a provisional plate crop (sent to the API; audit-logged)."""
    return vlm.second_reading(conn, user, sighting_id)


@router.post("/copilot/vlm/appearance/{alert_id}", tags=["copilot"])
def api_appearance(alert_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Police, image checks switched on: the vehicle's appearance vs the SIMULATED registry record."""
    return vlm.appearance(conn, user, alert_id)


class DecideIn(BaseModel):
    accept: bool
    reading: str = ""


@router.post("/copilot/vlm/{vlm_id}/decide", tags=["copilot"])
def api_vlm_decide(vlm_id: int, body: DecideIn, user: User = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    return vlm.decide(conn, user, vlm_id, body.accept, body.reading)


# ---------------------------------------------------------------------------- admin
@router.get("/copilot/settings", tags=["copilot"])
def api_copilot_settings(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    require(user, "admin")
    return {**config.status(conn), "models": [{"id": k, "label": v["label"], "price_usd_per_mtok": dict(zip(
        ("input", "output", "cache_write", "cache_read"), v["price"]))} for k, v in config.MODELS.items()],
            "efforts": list(config.EFFORTS), "usage": store.usage_summary(conn)}


class CopilotSettingsIn(BaseModel):
    model: Optional[str] = None
    effort: Optional[str] = None
    vlm_enabled: Optional[bool] = None


@router.put("/copilot/settings", tags=["copilot"])
def api_put_copilot_settings(body: CopilotSettingsIn, user: User = Depends(current_user),
                             conn: sqlite3.Connection = Depends(get_conn)):
    """Switch the model, the effort or the image checks (admin; audit-logged). The API key is never set here."""
    require(user, "admin")
    changes = body.model_dump(exclude_none=True)
    if "model" in changes and changes["model"] not in config.MODELS:
        raise ValueError(f"model must be one of {', '.join(config.MODELS)}")
    if "effort" in changes and changes["effort"] not in config.EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(config.EFFORTS)}")
    for k, v in changes.items():
        db.set_setting(conn, f"copilot_{k}", v)
    audit(conn, user, "copilot_settings", "settings", None, changes)
    return api_copilot_settings(user, conn)
