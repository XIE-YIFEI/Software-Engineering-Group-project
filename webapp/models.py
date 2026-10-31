"""Typed inputs and outputs of the service layer (pydantic). The API returns these as JSON, `/docs` documents
them, and the AI assistant (next task) can turn them into tool schemas."""
from __future__ import annotations

from typing import Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")
Provenance = Literal["camera", "inserted_plate", "synthetic"]

PROVENANCE_LABEL = {"camera": "", "inserted_plate": "Simulated · fictitious plate inserted",
                    "synthetic": "Simulated · synthetic (no vehicle in the video)"}


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int = 1
    page_size: int = 50


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    site_id: Optional[int] = None
    masked: bool


class VideoRef(BaseModel):
    """Where to play a record: `url` of the video and the second to seek to (record time - 1 s)."""
    url: str
    seek_sec: float
    kind: str = "annotated"          # annotated | source | timelapse


class AlertBrief(BaseModel):
    id: int
    code: str
    title: str
    severity: str
    rank: int
    message: str
    status: str


class CameraOut(BaseModel):
    id: int
    code: str
    name: str
    kind: str
    site: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    location_note: str = ""
    source_credit: str = ""
    status: str
    notional_clock: bool
    runs: int = 0
    sightings: int = 0
    synthetic: int = 0
    open_alerts: int = 0
    flow_total: Optional[int] = None
    last_seen: Optional[str] = None


class RunOut(BaseModel):
    id: int
    code: str
    label: str
    camera_id: int
    camera_name: str
    camera_kind: str
    origin: str
    media: str
    profile: Optional[str] = None
    fps: Optional[float] = None
    frames: Optional[int] = None
    window_start_sec: Optional[float] = None
    window_end_sec: Optional[float] = None
    clock_origin: str
    video_url: Optional[str] = None
    video_kind: Optional[str] = None
    video_offset_sec: float = 0.0
    image_url: Optional[str] = None
    provenance_note: str = ""
    flow_total: Optional[int] = None
    flow_by_type: Dict[str, int] = Field(default_factory=dict)
    registry_rules: Optional[List[str]] = None
    registry_disclaimer: Optional[str] = None
    results_path: str
    results_sha256: str
    imported_at: str


class SightingOut(BaseModel):
    id: int
    run_id: int
    run_label: str
    camera_id: int
    camera_name: str
    plate: str                       # full, or masked ("VJF ****") for masked roles
    masked: bool
    distance: Optional[float] = None  # fuzzy search: weighted edit distance to the query
    confidence: Optional[float] = None
    votes: Optional[int] = None
    status: str                      # CONFIRMED | TENTATIVE (shown as "Reading": provisional)
    valid_format: bool
    vtype: Optional[str] = None
    region: Optional[str] = None
    first_sec: float
    last_sec: float
    seen_at: str
    last_seen_at: str
    provenance: Provenance
    provenance_label: str = ""
    crop_url: Optional[str] = None
    alerts: List[AlertBrief] = Field(default_factory=list)
    video: Optional[VideoRef] = None


class RegistryRecord(BaseModel):
    plate: str
    body_type: Optional[str] = None
    colour: Optional[str] = None
    make_model: str = ""
    road_tax_expiry: Optional[str] = None
    status: str = "ok"
    note: str = ""
    simulated: bool = True


class SightingDetail(SightingOut):
    track_ids: List[int] = Field(default_factory=list)
    first_frame: Optional[int] = None
    last_frame: Optional[int] = None
    scene_url: Optional[str] = None
    registry: Optional[RegistryRecord] = None
    registry_name: str = ""
    registry_note: str = ""
    other_sightings: int = 0
    can_reveal: bool = False


class SightingQuery(BaseModel):
    """Filters of the plate search. `purpose` is required: it is the "why" of the audit log."""
    plate: str = ""
    tolerance: float = Field(1.0, ge=0, le=2)
    start: Optional[str] = None
    end: Optional[str] = None
    camera_id: Optional[int] = None
    run_id: Optional[int] = None
    vtype: Optional[str] = None
    status: Optional[str] = None
    provenance: Optional[str] = None      # camera | simulated (= inserted_plate + synthetic) | <one value>
    has_alerts: Optional[bool] = None
    purpose: str = ""
    reference: str = ""
    reveal_reason: Optional[str] = None   # masked roles searching by plate number
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=500)


class RevealOut(BaseModel):
    sighting_id: Optional[int] = None
    plate: str
    crop_url: Optional[str] = None
    expires_at: str


class TimelinePoint(BaseModel):
    seen_at: str
    place: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    provenance: Provenance
    provenance_label: str = ""
    source: str
    sighting_id: Optional[int] = None
    run_label: str = ""
    video: Optional[VideoRef] = None


class TimelineLeg(BaseModel):
    km: Optional[float] = None
    seconds: float
    speed_kmh: Optional[float] = None
    impossible: bool
    message: str


class Timeline(BaseModel):
    plate: str
    points: List[TimelinePoint]
    legs: List[TimelineLeg]
    impossible: bool
    rule: str
    simulated: bool
    near_plates: List[str] = Field(default_factory=list)


class AlertOut(BaseModel):
    id: int
    code: str
    title: str
    severity: str
    rank: int
    message: str
    status: str
    source: str
    provenance: Provenance
    provenance_label: str = ""
    seen_at: str
    camera_id: Optional[int] = None
    camera_name: str = ""
    run_id: Optional[int] = None
    run_label: str = ""
    plate: str = ""
    masked: bool = False
    sighting_id: Optional[int] = None
    lane_event_id: Optional[int] = None
    lane: Optional[str] = None
    crop_url: Optional[str] = None
    video: Optional[VideoRef] = None
    updated_at: str


class AlertAction(BaseModel):
    at: str
    user: str
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    note: str = ""
    corrected_plate: Optional[str] = None


class ComparisonRow(BaseModel):
    field: str
    registered: str
    observed: str
    match: Optional[bool] = None


class AlertDetail(AlertOut):
    history: List[AlertAction] = Field(default_factory=list)
    registry: Optional[RegistryRecord] = None
    comparison: List[ComparisonRow] = Field(default_factory=list)
    speed_text: Optional[str] = None
    value: Optional[float] = None
    error_1sigma: Optional[float] = None
    note: str = ""
    scene_url: Optional[str] = None
    allowed_transitions: List[str] = Field(default_factory=list)
    can_export: bool = False


class AlertCounts(BaseModel):
    by_status: Dict[str, int]
    by_rank: Dict[str, int]
    simulated: int
    total_open: int


class LaneRow(BaseModel):
    name: str
    group: str
    direction: str
    count: int
    by_type: Dict[str, int]
    veh_per_min: Optional[float] = None
    occupancy: Optional[float] = None
    density: Optional[float] = None
    density_unit: Optional[str] = None
    zone_length_m: Optional[float] = None
    mean_speed_kmh: Optional[float] = None
    speed_error_kmh: Optional[float] = None


class LaneEventOut(BaseModel):
    id: int
    code: str
    title: str
    severity: str
    time_sec: float
    seen_at: str
    lane: Optional[str] = None
    from_lane: Optional[str] = None
    message: str
    vtype: Optional[str] = None
    plate: str = ""
    provenance: Provenance
    provenance_label: str = ""
    value: Optional[float] = None
    error_1sigma: Optional[float] = None
    video: Optional[VideoRef] = None
    alert_id: Optional[int] = None


class TrafficSummary(BaseModel):
    run: RunOut
    total: int
    duration_sec: float
    veh_per_min: float
    by_type: Dict[str, int]
    lanes: List[LaneRow]
    outside_lanes: int = 0                     # crossings outside every lane polygon (e.g. a lay-by)
    groups: Dict[str, Dict[str, Optional[float]]]
    withheld: Dict[str, int]
    withheld_note: str = ""
    speed_note: str = ""


class Series(BaseModel):
    """x values (seconds or minutes) and one list of y values per series name (None = no value)."""
    x: List[float]
    x_label: str
    series: Dict[str, List[Optional[float]]]
    unit: str = ""


class SectionState(BaseModel):
    section: str
    label: str
    now: Optional[float] = None            # fill (0..1+) at the clock minute
    mean5: Optional[float] = None
    vehicles: Optional[float] = None
    capacity: Optional[float] = None
    state: str                             # free | dense | queue | no data
    forecast: Dict[str, Optional[float]] = Field(default_factory=dict)   # "5" / "15" / "30" -> fill
    band: Dict[str, float] = Field(default_factory=dict)
    status: str                            # Congested now | Congestion in ~15 min | Free flow ... (alpr.hud wording)
    status_colour: str
    warning: bool
    model: str = ""
    origin: str = ""


class ForecastDay(BaseModel):
    code: str
    label: str
    camera: str
    date: str
    first_minute: str
    last_minute: str
    calibrated: bool
    sections: List[str]
    train_day: Optional[str] = None
    model: Optional[str] = None
    first_warning: Optional[str] = None
    onsets: List[str] = Field(default_factory=list)
    video_url: Optional[str] = None
    video_from_minute: Optional[int] = None
    video_to_minute: Optional[int] = None
    video_duration_sec: Optional[float] = None
    loo_mae_vehicles: Optional[float] = None
    relative_mae: Optional[float] = None


class ForecastState(BaseModel):
    day: str
    at: str
    sections: List[SectionState]
    threshold: float


class Accuracy(BaseModel):
    day: str
    section: str
    horizon: int
    minutes: List[str]
    measured: List[Optional[float]]
    forecast: List[Optional[float]]        # made `horizon` minutes earlier for this minute
    mae: Optional[float] = None
    rmse: Optional[float] = None
    n: int
    band: Optional[float] = None
    threshold: float
    onsets: List[str] = Field(default_factory=list)
    warning_min: List[Optional[int]] = Field(default_factory=list)
    precision: Optional[float] = None
    recall: Optional[float] = None


class WhatIfOut(BaseModel):
    day: str
    section: str
    capacity_pct: float
    start_minute: str
    mu: float
    mu_source: str
    mu_estimate: Optional[float] = None
    mu_estimated_at: Optional[str] = None
    minutes: List[str]
    baseline: List[Optional[float]]
    projected: List[Optional[float]]
    threshold_vehicles: float
    capacity_vehicles: float
    congested_minutes: List[int]
    peak: List[float]
    assumptions: List[str]
    label: str = "Estimate under stated assumptions"


class Identity(BaseModel):
    """Gate identity of a plate, in the order banned > resident > valid pass > unregistered visitor."""
    plate: str
    identity: Literal["banned", "resident", "visitor_pre", "visitor_unregistered"]
    colour: Literal["red", "green", "teal", "yellow"]
    title: str
    detail: str
    unit_no: str = ""
    pass_id: Optional[int] = None
    pass_valid_to: Optional[str] = None
    simulated_list: bool = False


class GateCard(BaseModel):
    sighting_id: int
    plate: str
    vtype: Optional[str] = None
    first_sec: float
    video_time: float
    seen_at: str
    provenance: Provenance
    provenance_label: str = ""
    identity: Identity


class AccessEventOut(BaseModel):
    id: int
    at: str
    plate: str
    direction: str
    decision: str
    identity: str
    unit_no: str = ""
    pass_id: Optional[int] = None
    guard: str = ""
    note: str = ""
    simulated: bool = False
    sighting_id: Optional[int] = None


class InsideOut(BaseModel):
    plate: str
    identity: str
    unit_no: str = ""
    entered_at: str
    minutes_inside: int
    pass_valid_to: Optional[str] = None
    pass_expired: bool = False
    overstay: bool = False
    simulated: bool = False
    note: str = ""


class ListEntryOut(BaseModel):
    id: int
    list: str
    plate: str
    label: str = ""
    unit_no: str = ""
    reason: str = ""
    valid_to: Optional[str] = None
    simulated: bool = False
    created_by: str = ""
    created_at: str
    active: bool
    past_sightings: int = 0
    registry_status: Optional[str] = None


class PassOut(BaseModel):
    id: int
    plate: str
    unit_no: str
    visitor_label: str = ""
    valid_from: str
    valid_to: str
    status: str
    valid_now: bool
    simulated: bool = False
    created_by: str = ""


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    progress: float
    message: str
    original_name: str
    scene: str
    render_video: bool
    camera_id: int
    camera_name: str = ""
    recorded_at: str
    created_by: str = ""
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    run_id: Optional[int] = None
    error: Optional[str] = None


class AuditRow(BaseModel):
    id: int
    at: str
    username: str
    role: str
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    details: Dict
    reason: str = ""
    ip: Optional[str] = None


class Overview(BaseModel):
    date: str
    dates: List[str] = Field(default_factory=list)      # days with camera data, newest first
    demo_day: bool
    cameras: List[CameraOut]
    flow_total: int
    flow_by_type: Dict[str, int]
    flow_by_camera: Dict[str, int]
    sparklines: Dict[str, List[int]]
    alerts: AlertCounts
    top_alerts: List[AlertOut]
    congestion: Optional[ForecastState] = None
    congestion_day: Optional[ForecastDay] = None
    system: Optional[Dict[str, object]] = None
