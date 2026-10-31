"""Optional, local-only diagnostics for ALPR tracks without confirmed plates.

This module never changes the official plate log or registry alerts. Diagnostic
files may contain identifiable license plates; keep them in ignored output
folders, and do not publish them with a pull request.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Mapping

import cv2

from .voting import CONFIRMED


def collect_uncertain_tracks(states: Mapping[int, Any], voter: Any,
                             log: Iterable[Any] = (), *, start_frame: int = 0,
                             fps: float = 25.0) -> List[Dict[str, Any]]:
    """Summarise tracks with a plate reading but no confirmed log entry.

    ACQUIRING and TENTATIVE both count as unconfirmed. Tracks without OCR
    evidence, currently CONFIRMED, or already in the official log are excluded.
    The function does not mutate any pipeline state.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    logged_ids = {track_id for entry in log if not getattr(entry, "simulated", False)
                  for track_id in entry.track_ids}
    rows = []
    for track_id, state in sorted(states.items()):
        if track_id in logged_ids:
            continue
        result = voter.result(track_id)
        if result is None or result.status == CONFIRMED or result.votes <= 0:
            continue
        rows.append({
            "track_id": int(track_id),
            "status": result.status,
            "candidate_plate": result.compact,
            "confidence": result.confidence,
            "votes": result.votes,
            "ocr_attempts": state.n_reads,
            "first_frame": state.first_frame,
            "last_frame": state.last_frame,
            "first_sec": round((start_frame + state.first_frame) / fps, 2),
            "last_sec": round((start_frame + state.last_frame) / fps, 2),
        })
    return rows


def write_uncertain_diagnostics(states: Mapping[int, Any], voter: Any,
                                log: Iterable[Any], out_dir: str, *,
                                start_frame: int = 0, fps: float = 25.0) -> str:
    """Save a diagnostic-only JSON and best crops; return JSON path.

    The caller must explicitly opt in by passing an output directory. Files
    never enter the official plates.json report or the registry alert system.
    """
    rows = collect_uncertain_tracks(states, voter, log, start_frame=start_frame, fps=fps)
    os.makedirs(out_dir, exist_ok=True)
    for row in rows:
        crop = states[row["track_id"]].best_crop
        if crop is None or getattr(crop, "size", 0) == 0:
            continue
        filename = f"track_{row['track_id']:06d}.jpg"
        path = os.path.join(out_dir, filename)
        if not cv2.imwrite(path, crop):
            raise OSError(f"failed to write diagnostic crop: {path}")
        row["best_crop"] = filename
    report = {
        "schema_version": 1,
        "diagnostic_only": True,
        "warning": "Unconfirmed OCR candidates are not verified plate sightings or alerts.",
        "unconfirmed_tracks": rows,
    }
    report_path = os.path.join(out_dir, "uncertain_plates.json")
    with open(report_path, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)
    return report_path
