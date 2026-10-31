"""Paths and defaults of the web system.

Everything the system writes lives under one data folder, `data/web/` by default (gitignored, like the rest of
`data/`): the SQLite database, crops and scene frames, uploads, job folders and exports. Environment variables
override the defaults (the tests point SECS_WEB_DATA at a temporary folder):

    SECS_WEB_DATA       data folder                        (default <repo>/data/web)
    SECS_REGISTRY       SIMULATED vehicle registry (SQLite) (default <repo>/data/registry/demo_registry.db)
    SECS_PYTHON         interpreter the worker runs the pipeline with (default: this one)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent.parent          # repository root

# Defaults of the admin-editable settings (table `settings`). Retention periods are illustrative values for the
# course demo, chosen to follow the data-minimisation principle of Malaysia's PDPA 2010: keep ordinary sightings
# only as long as the task needs them.
DEFAULTS: Dict[str, object] = {
    "retention_days": 30,              # ordinary sightings (no alert, no list match) and their crops
    "retention_alert_days": 180,       # sightings with alerts: after the last alert is closed
    "retention_access_days": 90,       # gate access log
    "retention_corrections_days": 30,  # OCR correction pool
    "retention_audit_days": 365,       # audit log
    "overstay_hours": 4,               # visitors inside longer than this are pinned to the top
    "reveal_minutes": 15,              # how long a "Show full plate" lasts for one record
    "session_hours": 8,                # a login lasts this long
    "default_pass_hours": 4,           # default validity of a new visitor pass
    "max_speed_kmh": 250,              # cross-camera timeline: faster implied travel = impossible (as alpr.registry)
}

# Reasons offered for a search ("purpose", the "why" of the audit log) and for revealing a masked plate.
PURPOSES = ["Alert follow-up", "Investigation (case reference)", "Patrol check", "Gate operations",
            "Incident at site", "Traffic study", "Data-quality check", "Audit"]
REVEAL_REASONS = ["Accident investigation", "Incident review", "Data-quality check", "Audit", "Other"]


@dataclass
class Settings:
    data_dir: Path
    registry_path: Path
    python: str = field(default_factory=lambda: os.environ.get("SECS_PYTHON") or sys.executable)
    root: Path = ROOT

    @property
    def db_path(self) -> Path:
        return self.data_dir / "secs.db"

    @property
    def media_dir(self) -> Path:            # crops, blurred crops, scene frames, rendered plate images
        return self.data_dir / "media"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:             # one folder per analysis job (config, log, results)
        return self.data_dir / "jobs"

    @property
    def export_dir(self) -> Path:           # evidence packs and reports, as exported
        return self.data_dir / "exports"

    @property
    def corrections_dir(self) -> Path:      # OCR correction pool (crops), subject to retention
        return self.data_dir / "corrections"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.upload_dir, self.jobs_dir, self.export_dir,
                  self.corrections_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    data = Path(os.environ.get("SECS_WEB_DATA") or ROOT / "data" / "web")
    registry = Path(os.environ.get("SECS_REGISTRY") or ROOT / "data" / "registry" / "demo_registry.db")
    return Settings(data_dir=data.resolve(), registry_path=registry.resolve())


_active: "Settings | None" = None


def active() -> Settings:
    """The settings of the running app (set by webapp.app.create_app); loaded from the environment otherwise."""
    global _active
    if _active is None:
        _active = load_settings()
    return _active


def use(settings: Settings) -> None:
    global _active
    _active = settings
