"""Unit tests for the model-free ALPR logic (plates, slot decoding, voting, quality, lead, evaluate,
dataset labels, OCR reading order, registry rules and alerts, lanes, per-lane flow and the driving
behaviour rules on synthetic trajectories).

No weights, video or GPU needed:

    python alpr/tests/test_alpr.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from alpr.config import AlprConfig
from alpr.evaluate import edit_distance, evaluate
from alpr.lead import LeadSelector
from alpr.dataset import parse_label_line
from alpr.eval_images import match
from alpr.flow import LineCounter
from alpr.ocr import EasyPlateOCR
from alpr.plates import decode_slots, parse_plate
from alpr.quality import assess, sharpness
from alpr.registry import (BLACKLIST, DUPLICATE, EXPIRED, ILLEGAL_FORMAT, TYPE_MISMATCH, UNMATCHED, PlateChecker,
                           Registry, Sighting, VehicleRecord, build_demo_registry, check_travel, estimate_colour,
                           real_plates)
from alpr.voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateResult, PlateVoter


# ---------------------------------------------------------------- plates
def test_valid_formats():
    assert parse_plate("VJF1234").text == "VJF 1234"
    assert parse_plate("W1234A").text == "W 1234 A"
    assert parse_plate(["SAA", " 1234", "B"]).text == "SAA 1234 B"      # Sabah, OCR fragments
    assert parse_plate("qaa 1234 b").text == "QAA 1234 B"               # Sarawak, lower case
    assert parse_plate("PKA7").text == "PKA 7"                          # 1-digit number


def test_position_aware_correction():
    assert parse_plate("VJF1O34").text == "VJF 1034"                    # O in a digit slot -> 0
    assert parse_plate("VJF12B4").text == "VJF 1284"                    # B in a digit slot -> 8
    assert parse_plate("0JF1234").text == "OJF 1234"                    # 0 in a letter slot -> O
    p = parse_plate("WA1234")
    assert p.text == "WA 1234" and p.corrections == 0


def test_rejects_non_plates_without_a_blacklist():
    for junk in ["LUMPUR", "AIA", "KUALALUMPUR", "1234", "A", "", "PERSIARAN1", "12345678", "AB", "--"]:
        assert parse_plate(junk) is None, junk


def test_too_many_corrections_rejected():
    # 8 chars has exactly one legal layout (3 letters + 4 digits + 1 letter) and needs 3+ swaps
    assert parse_plate("0000OOOO", max_corrections=2) is None
    assert parse_plate("0O1O", max_corrections=2) is not None        # ambiguous, but readable


def test_edge_artefact_is_trimmed_instead_of_misparsed():
    """Seen on real video: border/emblem read as an extra first char ('QBDB4668' for 'BDB4668').
    The old 2-correction budget turned it into the wrong plate 'QBD 8466 B'."""
    p = parse_plate("QBDB4668")
    assert p.compact == "BDB4668" and p.trimmed == 1 and p.corrections == 0
    assert parse_plate("OBDB4668").compact == "BDB4668"
    assert parse_plate("VJF12341").compact == "VJF1234"             # trailing border read as '1'
    assert parse_plate("VJF1234").trimmed == 0                       # clean plates are never trimmed
    assert parse_plate("W1234A").text == "W 1234 A"                  # legit suffix is kept, not trimmed
    assert parse_plate("LUMPURA") is None                            # trimming must not rescue junk


def test_requires_a_genuine_digit():
    assert parse_plate("AIA") is None            # would otherwise become "AI 4"
    assert parse_plate("ABCDE") is None


def test_special_series():
    """Vanity series are legal plates, not junk to be trimmed ("GOLD1460" used to become "OLD 1460")."""
    p = parse_plate("GOLD1460")
    assert p.text == "GOLD 1460" and p.special and p.region == "Special series"
    assert parse_plate("VIPS6639").compact == "VIPS6639"
    assert parse_plate("IM4U555").text == "1M4U 555"                   # styled 'I' is the digit 1
    assert parse_plate("PUTRAJAYA1").text == "PUTRAJAYA 1"
    assert parse_plate("G0LD1522").compact == "GOLD1522"               # look-alike inside the word
    assert parse_plate("GOLD") is None                                 # word alone is not a plate


def test_region():
    assert parse_plate("JWT4811").region == "Johor"
    assert parse_plate("WA1234").region == "Kuala Lumpur"
    assert parse_plate("QAB218H").region == "Sarawak"
    assert parse_plate("HJA8055").region == "Taxi"
    assert parse_plate("KV2517A").region == "Langkawi (Kedah)"


ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"


def _slots(spec, n_slots=10):
    """spec: one {char: prob} dict per slot (remaining mass spread evenly); missing slots are pad."""
    p = np.full((n_slots, len(ALPHABET)), 1e-4)
    for i in range(n_slots):
        d = spec[i] if i < len(spec) else {"_": 0.99}
        for ch, v in d.items():
            p[i, ALPHABET.index(ch)] = v
    return p / p.sum(1, keepdims=True)


def test_slot_decoding_clean_plate():
    d = decode_slots(_slots([{c: 0.99} for c in "VJF1234"]), ALPHABET)
    assert d.valid and d.parse.text == "VJF 1234" and d.raw == "VJF1234" and d.conf > 0.95


def test_slot_decoding_uses_second_best_when_top1_is_illegal():
    # seen on a real crop: first slot O 0.87 / J 0.08; no Malaysian plate starts with O
    spec = [{"O": 0.87, "J": 0.08}] + [{c: 0.99} for c in "WT9605"]
    d = decode_slots(_slots(spec), ALPHABET)
    assert d.raw == "OWT9605" and d.parse.compact == "JWT9605"
    # a digit slot holding a letter falls back to the best digit
    spec = [{c: 0.99} for c in "WA1"] + [{"B": 0.6, "8": 0.35}] + [{c: 0.99} for c in "34"]
    assert decode_slots(_slots(spec), ALPHABET).parse.compact == "WA1834"


def test_slot_decoding_special_series_and_two_row():
    d = decode_slots(_slots([{c: 0.99} for c in "IM4U555"]), ALPHABET)   # model reads the styled I
    assert d.parse.text == "1M4U 555" and d.parse.special
    d = decode_slots(_slots([{c: 0.99} for c in "SAB1633C"]), ALPHABET)  # two-row plate, rows joined
    assert d.parse.text == "SAB 1633 C"


def test_slot_decoding_does_not_drop_a_confident_character():
    """Seen on video: 'WXY12345' with the 5th digit at 0.88 was decoded as the legal 'WXY1234'."""
    spec = [{c: 0.98} for c in "WXY1234"] + [{"5": 0.88, "_": 0.11}]
    d = decode_slots(_slots(spec), ALPHABET)
    assert not d.valid and d.raw == "WXY12345"
    # an unsure trailing character (border, bolt) may still be dropped
    spec = [{c: 0.98} for c in "WXY1234"] + [{"1": 0.3, "_": 0.6}]
    assert decode_slots(_slots(spec), ALPHABET).parse.compact == "WXY1234"


def test_slot_decoding_rejects_non_plates():
    d = decode_slots(_slots([{c: 0.99} for c in "PERODUA262"]), ALPHABET)   # dealer frame
    assert not d.valid and d.raw == "PERODUA262" and d.conf == 0.0


def test_easyocr_reading_order_two_rows():
    """Two-row plate KFQ / 1133: the old row key put both rows in one bucket -> '1133KFQ'."""
    seg = lambda x1, y1, x2, y2, t: ([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], t, 0.9)
    segs = [seg(20, 70, 110, 120, "1133"), seg(30, 10, 100, 60, "KFQ")]
    assert "".join(s[1] for s in EasyPlateOCR._reading_order(segs)) == "KFQ1133"
    segs = [seg(120, 12, 220, 58, "4811"), seg(10, 10, 100, 60, "JWT")]     # one row, two fragments
    assert "".join(s[1] for s in EasyPlateOCR._reading_order(segs)) == "JWT4811"


def test_label_lines_bbox_and_polygon():
    assert parse_label_line("0 0.5 0.5 0.2 0.1", 0) == (0.4, 0.45, 0.6, 0.55)
    poly = parse_label_line("1 0.1 0.2 0.3 0.2 0.3 0.4 0.1 0.4", 1)
    assert poly == (0.1, 0.2, 0.3, 0.4)
    assert parse_label_line("0 0.1 0.2 0.3 0.2 0.3 0.4", 1) is None           # other class (car)
    assert parse_label_line("[.ShellClassInfo]", 1) is None                   # desktop.ini junk


def test_box_matching_is_one_to_one():
    gts = [(0, 0, 10, 10), (20, 0, 30, 10)]
    preds = [(1, 0, 11, 10), (0, 0, 9, 10), (50, 50, 60, 60)]
    m = match(gts, preds)
    assert m == {0: 1} or m == {0: 0}
    assert len(set(m.values())) == len(m) and 1 not in m


# ---------------------------------------------------------------- voting
def test_voting_beats_last_reading_overwrite():
    """The failure seen in the real test: noisy frames, last reading wrong. Voting must still win."""
    v = PlateVoter(min_votes=3)
    reads = ["VJF1234", "VJF1234", "VJF1Z34", "VJF1234", "VJF7234", "VJF1234", "UJF1234"]
    for r in reads:
        v.add(1, r, conf=0.8, quality=0.8)
    res = v.result(1)
    assert res.compact == "VJF1234", res
    assert res.status == CONFIRMED


def test_charwise_consensus_when_no_single_string_repeats():
    v = PlateVoter(min_votes=3)
    for r in ["WA1234", "WA1284", "WA7234", "WX1234", "VA1234"]:    # each has a different single error
        v.add(1, r, conf=0.7)
    assert v.result(1).compact == "WA1234"


def test_status_progression_and_real_confidence():
    v = PlateVoter(min_votes=3, confirm_conf=0.6)
    assert v.result(9) is None
    v.add(9, "VJF1234", 0.9)
    assert v.result(9).status == ACQUIRING
    for _ in range(4):
        v.add(9, "VJF1234", 0.9)
    r = v.result(9)
    assert r.status == CONFIRMED and 0.85 <= r.confidence <= 0.9     # ~ the OCR confidence, not a constant


def test_inconsistent_plate_stays_tentative_with_low_confidence():
    v = PlateVoter(min_votes=3, confirm_conf=0.6)
    for r in ["ABC1111", "ABD2222", "ABE3333", "XYZ4444"]:
        v.add(2, r, 0.9)
    r = v.result(2)
    assert r.status == TENTATIVE and r.confidence < 0.6


def test_invalid_readings_are_ignored_and_tracks_are_independent():
    v = PlateVoter()
    assert v.add(1, "LUMPUR", 0.99) is None
    assert v.result(1) is None
    v.add(1, "VJF1234", 0.9); v.add(2, "WA5678", 0.9)
    assert v.result(1).compact == "VJF1234" and v.result(2).compact == "WA5678"


def test_low_confidence_readings_are_outvoted():
    v = PlateVoter()
    for _ in range(4):
        v.add(1, "VJF1234", 0.9, 0.9)
    for _ in range(3):
        v.add(1, "VJF1284", 0.2, 0.2)
    assert v.result(1).compact == "VJF1234"


def test_one_ambiguous_character_blocks_confirmation():
    """Seen on video: 'VCH 1587' vs 'VCR 1587' at ~50/50 flipped between two CONFIRMED plates."""
    v = PlateVoter(min_votes=3)
    for r in ["VCH1587", "VCR1587"] * 4:
        v.add(1, r, conf=0.95)
    assert v.result(1).status == TENTATIVE
    for _ in range(8):
        v.add(1, "VCH1587", conf=0.95)
    r = v.result(1)
    assert r.compact == "VCH1587" and r.status == CONFIRMED


def test_nonstandard_plate_is_not_repaired_into_a_legal_one():
    """'WXY12345' (five digits) used to be trimmed to the legal 'WXY 1234' and reported as valid."""
    v = PlateVoter(min_votes=3)
    for _ in range(6):
        assert v.add(1, "WXY12345", conf=0.95, quality=0.9, valid=False) is None
    r = v.result(1)
    assert r.compact == "WXY12345" and r.text == "WXY 12345" and not r.valid and r.status == CONFIRMED
    v.add(2, "WXY12345", conf=0.95)                     # no OCR verdict (EasyOCR path): old parsing
    assert v.result(2).compact == "WXY1234" and v.result(2).valid


def test_far_junk_does_not_override_a_clear_legal_plate():
    v = PlateVoter(min_votes=3)
    for _ in range(3):
        v.add(1, "QQ7", conf=0.6, quality=0.05, valid=False)          # far, blurry
    for _ in range(6):
        v.add(1, "VCV155", conf=0.95, quality=0.9, valid=True)
    r = v.result(1)
    assert r.compact == "VCV155" and r.valid and r.status == CONFIRMED


def test_display_nonstandard():
    from alpr.voting import display_nonstandard
    assert display_nonstandard("WXY12345") == "WXY 12345"
    assert display_nonstandard("FAKE1A") == "FAKE 1 A"


def test_render_and_paste_plate_only_touch_the_box():
    from alpr.simulate import paste_plate, render_plate
    plate = render_plate("XSB 2002")
    assert plate.shape[0] < plate.shape[1] and plate.mean() < 80          # dark plate, light characters
    assert render_plate("XSB 2002", two_row=True).shape[0] > plate.shape[0]
    frame = np.full((200, 300, 3), 120, np.uint8)
    before = frame.copy()
    paste_plate(frame, (100, 80, 200, 110), plate)
    changed = np.argwhere((frame != before).any(axis=2))
    assert changed.size and changed[:, 0].min() >= 80 and changed[:, 0].max() < 110
    assert changed[:, 1].min() >= 100 and changed[:, 1].max() < 200


# ---------------------------------------------------------------- quality
def _plate_like(w=120, h=40):
    img = np.full((h, w, 3), 235, np.uint8)
    cv2.putText(img, "VJF1234", (4, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (10, 10, 10), 2)
    return img


def test_blur_lowers_sharpness_and_score():
    sharp = _plate_like()
    blurry = cv2.GaussianBlur(sharp, (0, 0), 3.0)
    assert sharpness(sharp) > 3 * sharpness(blurry)
    qs, qb = assess(sharp), assess(blurry)
    assert qs.score > qb.score and qs.ok and not qb.ok


def test_size_and_exposure_gates():
    assert not assess(_plate_like(20, 8)).ok                            # too small
    assert not assess(np.full((40, 120, 3), 2, np.uint8)).ok            # black
    assert not assess(np.zeros((0, 0, 3), np.uint8)).ok                 # empty


def test_sharpness_is_size_normalised():
    a = sharpness(_plate_like(120, 40))
    b = sharpness(cv2.resize(_plate_like(120, 40), (360, 120), interpolation=cv2.INTER_CUBIC))
    assert 0.3 < a / max(b, 1e-6) < 6.0


# ---------------------------------------------------------------- lead
def _veh(tid, x1, y1, x2, y2):
    return {"track_id": tid, "box": [x1, y1, x2, y2]}


def test_lead_prefers_ego_lane_over_slightly_bigger_side_vehicle():
    sel = LeadSelector(lane_weight=0.6, hysteresis=1.25)
    centre = _veh(1, 800, 500, 1100, 700)       # 300x200 in the middle
    side = _veh(2, 1500, 450, 1850, 700)        # 350x250 but far right
    assert sel.pick([centre, side], 1920, 1080) == 1


def test_lead_hysteresis_prevents_flicker():
    sel = LeadSelector(lane_weight=0.0, hysteresis=1.25)
    a, b = _veh(1, 100, 100, 500, 400), _veh(2, 600, 100, 1000, 420)    # b marginally larger
    assert sel.pick([a], 1920, 1080) == 1
    assert sel.pick([a, b], 1920, 1080) == 1                            # stays
    big = _veh(2, 600, 100, 1300, 600)                                  # much larger -> switch
    assert sel.pick([a, big], 1920, 1080) == 2


def test_lead_min_width_and_empty():
    sel = LeadSelector(min_width_frac=0.1)
    assert sel.pick([], 1920, 1080) is None
    assert sel.pick([_veh(1, 0, 0, 100, 100)], 1920, 1080) is None      # 100 < 192


def test_ligature_resolution_vj_from_w_and_v():
    """In slanted/italic Malaysian fonts, 'VJ' is frequently read as 'W' or 'V'.
    When both 'WF' and 'VF' (or 'VJF') co-occur, they must align to 'VJF 2627'."""
    v = PlateVoter(min_votes=3, resolve_ligatures=True)
    reads = ["WF2627", "WF2627", "VF2627", "VF2627", "WF2627"]
    for r in reads:
        v.add(1, r, conf=0.8)
    res = v.result(1)
    assert res.compact == "VJF2627" and res.text == "VJF 2627"
    assert res.status == CONFIRMED

    # When higher-resolution reading VJF2627 is present alongside WF2627:
    v2 = PlateVoter(min_votes=3, resolve_ligatures=True)
    for r in ["WF2627", "WF2627", "WF2627", "VJF2627"]:
        v2.add(2, r, conf=0.8)
    res2 = v2.result(2)
    assert res2.compact == "VJF2627" and res2.text == "VJF 2627"


# ---------------------------------------------------------------- evaluate
def test_edit_distance():
    assert edit_distance("VJF1234", "VJF1234") == 0
    assert edit_distance("VJF1234", "VJF1284") == 1
    assert edit_distance("", "AB") == 2


def test_evaluate_matches_and_counts_false_positives():
    rep = evaluate(["VJF 1234", "WA 5678", "ZZZ 9999"], ["VJF1234", "WA5679"])
    assert rep["n_gt"] == 2 and rep["exact_match"] == 0.5
    assert abs(rep["mean_cer"] - (0 + 1 / 6) / 2) < 1e-9
    assert rep["false_positives"] == 1
    assert evaluate([], ["VJF1234"])["mean_cer"] == 1.0


# ---------------------------------------------------------------- flow counting
def test_line_counter_counts_each_vehicle_once_with_direction():
    lc = LineCounter(y_frac=0.5)                       # line at y=500 on a 1000 px frame
    for f, y2 in enumerate([300, 420, 480, 510, 560, 490, 530]):   # crosses, jitters back and forth
        lc.update(1, (0, 0, 10, y2), "Car", f, 1000)
    for f, y2 in enumerate([700, 600, 450]):           # moving up the image
        lc.update(2, (0, 0, 10, y2), "Truck", f + 10, 1000)
    lc.update(3, (0, 0, 10, 520), "Car", 20, 1000)     # first seen below the line: not a crossing
    assert lc.total == 2 and lc.by_type() == {"Car": 1, "Truck": 1}
    assert [c.direction for c in lc.crossings] == ["inbound", "outbound"]


def test_line_counter_rate_and_time_series():
    lc = LineCounter(0.5)
    for tid, f in enumerate([30, 60, 400]):            # crossings at 1 s, 2 s, 13.3 s (30 FPS)
        lc.update(tid, (0, 0, 1, 100), "Car", f - 1, 1000)
        lc.update(tid, (0, 0, 1, 900), "Car", f, 1000)
    ts = lc.time_series(fps=30, bin_sec=10)
    assert [r["total"] for r in ts] == [2, 1] and ts[1]["t_start"] == 10
    assert abs(lc.rate_per_min(frame=600, fps=30, window_sec=60) - 3 * 60 / 20) < 1e-9


def test_video_evaluate_ignores_unverifiable_and_counts_duplicates():
    rep = evaluate(["WRS7607", "WRS7607", "MBDD67", "WWD6711"], ["WRS7607", "WWD6737"], ignore=["MBD067"])
    assert rep["exact_match"] == 0.5 and rep["false_positives"] == 1          # the duplicate WRS7607
    assert [r["pred"] for r in rep["rows"]] == ["WRS7607", "WWD6711"]


def test_same_vehicle_plate_matching():
    """Two tracks of one vehicle: identical or near-identical readings merge, different plates do not."""
    from alpr.pipeline import _same_plate
    assert _same_plate("WLK6994", "VUK6994")          # one fragment misread two characters
    assert _same_plate("VN683", "VN1683")             # dropped digit
    assert not _same_plate("WB7605J", "WRS7607")      # different cars
    assert not _same_plate("QMU79", "WB79")           # short plates need a closer match


# ---------------------------------------------------------------- registry checks
NOW = datetime(2026, 10, 14, 8, 0)
KL, JB = (3.1390, 101.6869), (1.4927, 103.7414)        # ~300 km apart


def _registry():
    reg = Registry()                                   # in-memory
    reg.add([VehicleRecord("WA1234B", "Sedan", "Silver", "Proton Saga", date(2027, 1, 31)),
             VehicleRecord("VJF1234", "Hatchback", "Red", road_tax_expiry=date(2026, 9, 30)),
             VehicleRecord("BMW319", "Sedan", status="blacklisted", note="reported stolen"),
             VehicleRecord("SAB1633C", "Motorcycle"),
             VehicleRecord("V1108", "Pickup")])
    reg.add_sightings([Sighting("WA1234B", datetime(2026, 10, 14, 7, 45), "Johor Bahru", *JB)])
    return reg


def _codes(alerts):
    return [a.code for a in alerts]


def test_registry_unmatched_and_illegal_format():
    chk = PlateChecker(_registry())
    assert chk.check("SAB 1633 C", when=NOW, vtype="Motorcycle") == []          # spaces / case do not matter
    assert _codes(chk.check("WXY1234", when=NOW)) == [UNMATCHED]
    assert _codes(chk.check("LUMPUR", when=NOW)) == [ILLEGAL_FORMAT]            # no registry rule on non-plates
    assert _codes(chk.check("VJF1234", valid=False, when=NOW)) == [ILLEGAL_FORMAT]   # the OCR's verdict wins


def test_one_character_off_a_registered_plate_is_a_misread_not_unmatched():
    """Seen on the demo video: VCH 1587 was briefly confirmed as VCR 1587, VN 1683 as VW 1683."""
    reg = _registry()
    assert reg.near("WA1284B") == ["WA1234B"] and reg.near("WA123B") == ["WA1234B"] and reg.near("WXY1234") == []
    chk = PlateChecker(reg)
    assert chk.check("WA1284B", when=NOW) == []                                 # probable misread
    assert chk.check("BMW318", when=NOW, vtype="Car") == []                     # not blamed for BMW319's record
    assert _codes(PlateChecker(reg, misread_tolerance=False).check("WA1284B", when=NOW)) == [UNMATCHED]


def test_registry_expiry_uses_the_observation_time():
    chk = PlateChecker(_registry())
    assert chk.check("VJF1234", when=datetime(2026, 9, 30, 23, 59)) == []      # last valid day
    a = chk.check("VJF1234", when=datetime(2026, 10, 2, 9, 0))
    assert _codes(a) == [EXPIRED] and "2026-09-30" in a[0].message and a[0].severity == "medium"


def test_registry_blacklist_and_vehicle_type():
    chk = PlateChecker(_registry())
    a = chk.check("BMW319", when=NOW, vtype="Car")
    assert _codes(a) == [BLACKLIST] and a[0].severity == "high" and a[0].message == "reported stolen"
    assert _codes(chk.check("SAB1633C", when=NOW, vtype="Car")) == [TYPE_MISMATCH]   # motorcycle plate on a car
    assert chk.check("V1108", when=NOW, vtype="Truck") == []                  # pickups are often "truck"
    assert chk.check("SAB1633C", when=NOW, vtype=None) == []                  # photo: type unknown, not checked
    a = PlateChecker(_registry()).check("BMW319", when=NOW, vtype="Motorcycle")
    assert _codes(a) == [BLACKLIST, TYPE_MISMATCH]                            # most severe first


def test_registry_rules_can_be_switched_off():
    chk = PlateChecker(_registry(), rules=["EXPIRED"])
    assert chk.check("WXY1234", when=NOW) == [] and chk.check("LUMPUR", when=NOW) == []
    try:
        PlateChecker(_registry(), rules=["STOLEN"])
        assert False, "unknown rule accepted"
    except ValueError:
        pass


def test_duplicate_by_impossible_travel_and_by_two_vehicles_in_view():
    chk = PlateChecker(_registry(), camera=("KL camera", *KL))
    a = chk.check("WA1234B", when=NOW, vtype="Car")             # Johor Bahru 07:45 -> KL 08:00: ~1200 km/h
    assert _codes(a) == [DUPLICATE] and "Johor Bahru" in a[0].message
    assert chk.check("WA1234B", when=datetime(2026, 10, 14, 12, 0), vtype="Car") == []   # 4 h later: fine
    assert check_travel(NOW, KL, [Sighting("X1", NOW, "same site", 3.1395, 101.6870)]) is None
    a = chk.check("WXY1234", when=NOW, coexisting_tracks=[7, 3])
    assert _codes(a) == [DUPLICATE, UNMATCHED] and "#3, #7" in a[0].message


def test_colour_estimate_and_tolerant_colour_check():
    solid = lambda bgr: np.full((80, 120, 3), bgr, np.uint8)
    assert estimate_colour(solid((40, 40, 200))) == "Red"
    assert estimate_colour(solid((200, 90, 30))) == "Blue"
    assert estimate_colour(solid((245, 245, 245))) == "White"
    assert estimate_colour(solid((20, 20, 20))) == "Black"
    car = solid((40, 40, 200))
    car[40:60, 30:90] = 15                                      # black grille / windows: body colour still wins
    assert estimate_colour(car) == "Red"
    assert estimate_colour(np.zeros((4, 4, 3), np.uint8)) is None
    chk = PlateChecker(_registry(), check_colour=True)
    assert chk.check("WA1234B", when=NOW, vtype="Car", colour="White") == []        # silver ~ white
    assert _codes(chk.check("WA1234B", when=NOW, vtype="Car", colour="Red")) == [TYPE_MISMATCH]
    assert PlateChecker(_registry()).check("WA1234B", when=NOW, vtype="Car", colour="Red") == []   # off by default


def test_demo_registry_never_flags_a_real_plate():
    """The repo is public: every real plate in alpr/gt is a neutral placeholder no rule can flag, and
    every fictitious record uses the X series (no state uses X)."""
    reg = build_demo_registry(":memory:")
    chk = PlateChecker(reg, check_colour=True, camera=("demo", *KL))
    plates = real_plates()
    assert len(plates) > 100 and not any(p.startswith("X") for p in plates)
    for p in plates:
        for vtype in ("Car", "Motorcycle", "Bus", "Truck"):
            assert chk.check(p, when=datetime(2099, 1, 1), vtype=vtype, colour="Red") == [], (p, vtype)
    fictitious = [r[0] for r in reg.db.execute("SELECT plate FROM vehicles WHERE simulated = 1")]
    assert len(fictitious) >= 300 and all(p.startswith("X") for p in fictitious)
    assert not any(edit_distance(f, p) <= 1 for f in fictitious for p in plates)   # a misread cannot hit one
    assert chk.check("VCR1587", when=NOW, vtype="Car") == []                    # demo misread of VCH 1587
    assert _codes(chk.check("XSB2002", when=NOW, vtype="Car")) == [BLACKLIST]
    assert _codes(chk.check("XSA1001", when=NOW, vtype="Car")) == [EXPIRED, TYPE_MISMATCH]
    assert _codes(chk.check("XSE5005", when=NOW, vtype="Car")) == [UNMATCHED]
    assert _codes(chk.check("XSD4004", when=datetime(2026, 10, 14, 8, 2), vtype="Car")) == [DUPLICATE]


def _bare_pipeline(**cfg):
    """An AlprPipeline with only the plate-log / registry state (no models)."""
    from alpr.pipeline import AlprPipeline
    p = AlprPipeline.__new__(AlprPipeline)
    p.cfg, p.fps, p.log = AlprConfig(**cfg), 30.0, []
    p.checker, p.clock0 = PlateChecker(_registry()), NOW
    p._reset_apart()
    p.states, p._sim_pending = {}, list(p.cfg.registry_sim_events)
    return p


def _car_track():
    from alpr.pipeline import TrackState
    st = TrackState(1, 1)
    st.cls_conf[2] = 1.0
    return st


def test_two_vehicles_in_view_with_one_plate_are_two_entries_and_a_duplicate():
    p, st1, st2 = _bare_pipeline(), _car_track(), _car_track()
    res = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
    boxes = {1: [0, 0, 100, 100], 2: [300, 0, 400, 100]}               # side by side
    for idx in range(1, 10):
        p._update_log(1, st1, res, idx, boxes)
        p._update_log(2, st2, res, idx, boxes)
    assert len(p.log) == 2 and [sorted(e.track_ids) for e in p.log] == [[1], [2]]
    assert all(_codes(e.alerts) == [DUPLICATE] for e in p.log)


def test_reacquired_or_double_boxed_vehicle_is_still_one_entry():
    res = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
    p, st1, st2 = _bare_pipeline(), _car_track(), _car_track()
    p._update_log(1, st1, res, 1, {1: [0, 0, 100, 100]})
    p._update_log(2, st2, res, 40, {2: [0, 0, 100, 100]})                # track 1 lost, vehicle re-acquired
    assert len(p.log) == 1 and p.log[0].track_ids == {1, 2} and p.log[0].alerts == []
    p, st1, st2 = _bare_pipeline(), _car_track(), _car_track()
    boxes = {1: [0, 0, 100, 100], 2: [10, 10, 90, 95]}                 # two boxes on one vehicle
    for idx in range(1, 10):
        p._update_log(1, st1, res, idx, boxes)
        p._update_log(2, st2, res, idx, boxes)
    assert len(p.log) == 1 and p.log[0].alerts == []


def test_passing_misread_does_not_raise_an_alert():
    p, st = _bare_pipeline(), _car_track()
    p._update_log(1, st, PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED), 1, {1: [0, 0, 100, 100]})
    p._update_log(1, st, PlateResult("PKA 7", "PKA7", 0.5, 12, TENTATIVE), 2, {1: [0, 0, 100, 100]})
    assert p.log[0].result.compact == "PKA7" and p.log[0].alerts == []          # not CONFIRMED: not checked
    p._update_log(1, st, PlateResult("PKA 7", "PKA7", 0.9, 14, CONFIRMED), 3, {1: [0, 0, 100, 100]})
    assert _codes(p.log[0].alerts) == [UNMATCHED]


def test_simulated_events_are_logged_apart_from_camera_plates():
    p = _bare_pipeline(registry_sim_events=[{"at_sec": 1.0, "plate": "BMW 319", "vtype": "Car"}])
    p._inject_sim_events(15)                                            # 0.5 s: not yet
    assert p.log == []
    p._inject_sim_events(30)
    e, = p.log
    assert e.simulated and e.result.text == "BMW 319" and _codes(e.alerts) == [BLACKLIST]
    assert e.crop is not None and e.crop.shape[0] == 46                 # synthetic plate thumbnail
    assert p.summary() == [] and p.simulated_events()[0]["alerts"][0]["code"] == BLACKLIST


def test_dashboard_renders_alerts():
    from alpr.hud import render_dashboard
    from alpr.pipeline import LogEntry
    from alpr.registry import Alert
    cfg = AlprConfig(hud_mode="dashboard", count_line_y=0.5)
    blk = Alert(BLACKLIST, "high", "SIMULATED: reported stolen")
    res = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
    vehicles = [{"track_id": 1, "box": [100, 100, 300, 300], "vtype": "Car", "plate_box": None, "result": res,
                 "alerts": [blk]}]
    log = [LogEntry(res, "Car", "Kuala Lumpur", 1, 5, {1}, alerts=[blk]),
           LogEntry(res, "Car", "", 2, 4, simulated=True, alerts=[blk])]
    stats = {"fps": 5.0, "fps_video": 30.0, "time_sec": 1.0, "count": 0, "by_type": {}, "rate_per_min": 0.0,
             "plates": 1, "confirmed": 1, "alerts": 2}
    out = render_dashboard(np.full((480, 640, 3), 90, np.uint8), vehicles, log, stats, cfg)
    assert out.shape == (1080, 1920, 3)
    b, g, r = (out[..., k].astype(int) for k in range(3))
    red = (r > 200) & (g < 110) & (b < 110)                             # high-alert red (system red)
    assert red[:, 1468:].any() and red[:, :1444].any()                  # log card pill + vehicle alert pill


def test_rounded_masks_are_antialiased_and_bounded():
    from alpr.hud import _ring_mask, _rr_mask, fill
    m = _rr_mask(40, 20, 8)
    assert m.shape == (20, 40) and m[10, 20] == 1.0 and m[0, 0] == 0.0
    assert 0.0 < m[1, 3] < 1.0                                          # soft corner edge
    ring = _ring_mask(40, 20, 8, 2)
    assert ring[10, 20] == 0.0 and ring[10, 0] > 0.9
    img = np.zeros((30, 30, 3), np.uint8)
    fill(img, (-10, -10, 15, 15), 4, (255, 0, 0))                       # partly outside: clipped, no error
    assert img[5, 5, 2] == 255 and img[20, 20].sum() == 0


def test_video_evaluate_scores_expected_alerts():
    gts = ["XSA1001", "VCV155", "XSE5005"]
    want = [{"EXPIRED", "TYPE_MISMATCH"}, set(), {"UNMATCHED"}]
    preds = ["XSA1001", "VCV155", "XSE5005"]
    got = [{"EXPIRED", "TYPE_MISMATCH"}, {"UNMATCHED"}, set()]       # a real plate flagged, a violation missed
    a = evaluate(preds, gts, (), got, want)["alerts"]
    assert a == {"violations": 2, "violations_caught": 1, "valid_plates": 1, "false_alerts": 1,
                 "false_alerts_on_unmatched": 0}


def test_demo_registry_rebuilds_when_real_plates_change():
    import tempfile
    from alpr import registry as R
    d = tempfile.mkdtemp()
    gt = os.path.join(d, "gt")
    os.makedirs(gt)
    with open(os.path.join(gt, "a.txt"), "w", encoding="utf-8") as f:
        f.write("WA1234B\n")
    old_dir, R.GT_DIR = R.GT_DIR, gt
    try:
        db = os.path.join(d, "reg.db")
        R.build_demo_registry(db, n_fictitious=5).close()
        with open(os.path.join(gt, "a.txt"), "a", encoding="utf-8") as f:
            f.write("VCV155\n")                                       # a new real plate is labelled
        reg = R.Registry.open(db)                                      # -> rebuilt automatically
        assert reg.lookup("VCV155") is not None and not reg.lookup("VCV155").simulated
        reg.close()
    finally:
        R.GT_DIR = old_dir


# ---------------------------------------------------------------- lanes + behaviour rules (synthetic)
# A 1000 x 1000 frame: inbound lanes A1 (x 500-600) and A2 (x 600-700) travel down the image, outbound
# B1 (x 350-450) travels up; x > 700 is a lay-by (not a lane). A solid line separates A1 | A2 between
# y 600 and 900 (dashed above). Road calibration: 3.5 m per 100 px across, 20 px per metre along.
_LANES = [{"name": "A1", "group": "In", "direction": "down", "polygon": [[.5, .2], [.6, .2], [.6, 1], [.5, 1]]},
          {"name": "A2", "group": "In", "direction": "down", "polygon": [[.6, .2], [.7, .2], [.7, 1], [.6, 1]]},
          {"name": "B1", "group": "Out", "direction": "up", "polygon": [[.35, .2], [.45, .2], [.45, 1], [.35, 1]]}]
_SOLID = [{"name": "A1|A2 solid", "points": [[.6, .6], [.6, .9]]}]
_CAL = {"image_points": [[.5, 1.0], [.7, 1.0], [.7, .2], [.5, .2]],
        "road_points": [[0.0, 0.0], [7.0, 0.0], [7.0, 40.0], [0.0, 40.0]]}


def _lane_map():
    from alpr.lanes import LaneMap
    return LaneMap.from_config(_LANES, _SOLID, 1000, 1000)


def _box(x, y, w=60, h=50):
    """Box with its bottom-centre at (x, y)."""
    return [x - w / 2, y - h, x + w / 2, y]


def _monitor(cal=False, **p):
    from alpr.behaviour import BehaviourMonitor, RuleParams
    from alpr.lanes import Calibration
    c = Calibration.from_config(_CAL, 1000, 1000) if cal else None
    return BehaviourMonitor(_lane_map(), 30.0, RuleParams(**p), c, 1000)


def _drive(mon, tracks, n):
    """tracks: {track_id: f(frame) -> (x, y) or None, vtype?}. Runs n frames; returns all new events."""
    out = []
    for f in range(1, n + 1):
        vs = []
        for tid, spec in tracks.items():
            fn, vtype, sim = (spec + ("Car", False))[:3] if isinstance(spec, tuple) else (spec, "Car", False)
            p = fn(f)
            if p is not None:
                vs.append({"track_id": tid, "box": _box(*p), "vtype": vtype, "simulated": sim})
        out += mon.update(f, vs)
    return out


def test_lane_geometry_polygon_side_and_reference_point():
    from alpr.lanes import LaneMap, point_in_polygon, side_of_polyline
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon((5, 5), sq) and not point_in_polygon((15, 5), sq)
    line = [(0, 0), (0, 10)]
    assert side_of_polyline((1, 5), line) == -side_of_polyline((-1, 5), line) != 0
    assert side_of_polyline((1, 20), line) == 0                      # beyond the end of the line
    lm = _lane_map()
    assert lm.lane_at((550, 500)).name == "A1" and lm.lane_at((800, 500)) is None
    assert lm.by_name["A1"].axis[1] > 0.99 and lm.by_name["B1"].axis[1] < -0.99
    # perspective correction: points move away from the vanishing point, more near the camera
    lm2 = LaneMap.from_config(_LANES, [], 1000, 1000, vanishing_point=[0.5, 0.1], ref_shift=0.3)
    far, near = lm2.ref_point(_box(650, 300)), lm2.ref_point(_box(650, 900))
    assert 650 < far[0] < near[0] and lm2.ref_point(_box(500, 900))[0] == 500


def test_lane_assigner_ignores_jitter_on_a_line_and_needs_time():
    from alpr.lanes import LaneAssigner
    la = LaneAssigner(_lane_map(), min_frames=6, margin=0.12)
    changes = [la.update(1, _box(600 + (3 if f % 2 else -3), 500), f)[1] for f in range(40)]
    assert not any(changes)                                          # riding the A1|A2 line: no change
    la = LaneAssigner(_lane_map(), min_frames=6, margin=0.12)
    lanes, changes = [], []
    for f in range(30):
        lane, ch = la.update(2, _box(550 if f < 12 else 650, 500), f)
        lanes.append(lane)
        if ch:
            changes.append(ch)
    assert lanes[4] is None and lanes[5] == "A1"                     # first lane after 6 frames
    assert changes == [("A1", "A2")] and lanes[16] == "A1" and lanes[17] == "A2"


def test_lane_change_event_and_solid_line_crossing():
    from alpr.behaviour import LANE_CHANGE, SOLID_LINE
    mon = _monitor()
    ev = _drive(mon, {1: lambda f: (550 if f < 20 else 650, 300 + 2 * f),       # dashed part: change only
                      2: lambda f: (550 if f < 20 else 650, 650 + 2 * f)}, 60)   # solid part
    by = {(e.track_id, e.code) for e in ev}
    assert by == {(1, LANE_CHANGE), (2, LANE_CHANGE), (2, SOLID_LINE)}
    lc = next(e for e in ev if e.code == LANE_CHANGE and e.track_id == 1)
    assert (lc.from_lane, lc.lane, lc.severity) == ("A1", "A2", "info")
    solid = next(e for e in ev if e.code == SOLID_LINE)
    assert solid.severity == "violation" and solid.withheld                      # a camera vehicle
    mon = _monitor()
    ev = _drive(mon, {3: lambda f: (600 + (4 if f % 2 else -4), 650 + 2 * f)}, 60)  # on the solid line
    assert ev == []
    mon = _monitor()                                                    # motorcycles filter between lanes
    ev = _drive(mon, {4: (lambda f: (550 if f < 20 else 650, 300 + 2 * f), "Motorcycle")}, 60)
    assert ev == []


def test_wrong_way_only_against_the_lane_direction():
    from alpr.behaviour import WRONG_WAY
    mon = _monitor()
    ev = _drive(mon, {1: lambda f: (550, 900 - 6 * f),                  # A1 (down) driven upwards
                      2: lambda f: (650, 300 + 6 * f),                  # A2 normal
                      3: lambda f: (400, 900 - 6 * f),                  # B1 (up) normal
                      4: lambda f: (650, 600 + (2 if f % 2 else -2))}, 90)   # jittering in place
    ww = [e for e in ev if e.code == WRONG_WAY]
    assert [e.track_id for e in ww] == [1] and ww[0].lane == "A1"
    mon = _monitor()                                                    # leaves the lanes, then turns back
    ev = _drive(mon, {5: lambda f: (650, 300 + 6 * f) if f < 60 else (850, 700 - 6 * (f - 60))}, 120)
    assert not [e for e in ev if e.code == WRONG_WAY]


def test_stopped_in_lane_vs_queue_vs_lay_by():
    from alpr.behaviour import STOPPED
    flowing = {k: (lambda k: lambda f: (650, 200 + (8 * f + 150 * k) % 800))(k) for k in range(10, 14)}
    mon = _monitor(stopped_sec=4.0)
    ev = _drive(mon, {1: lambda f: (550, 600 + min(f, 10)), **flowing}, 200)
    st = [e for e in ev if e.code == STOPPED]
    assert [e.track_id for e in st] == [1] and st[0].severity == "hazard" and not st[0].withheld
    queue = {k: (lambda k: lambda f: (650, 300 + 100 * k))(k) for k in range(1, 4)}     # everyone stopped
    mon = _monitor(stopped_sec=4.0)
    assert not [e for e in _drive(mon, {9: lambda f: (550, 600), **queue}, 200) if e.code == STOPPED]
    mon = _monitor(stopped_sec=4.0)                                      # parked in the lay-by
    assert not [e for e in _drive(mon, {9: lambda f: (850, 600), **flowing}, 200) if e.code == STOPPED]


def test_calibrated_speed_has_an_error_bar_and_speeding_uses_its_lower_bound():
    from alpr.behaviour import SPEEDING
    from alpr.lanes import Calibration
    cal = Calibration.from_config(_CAL, 1000, 1000)
    assert 0.02 < cal.rel_sigma < 0.15
    t = [k / 30 for k in range(30)]
    pts = [(550, 300 + 20 * 25 * tk) for tk in t]                       # 25 m/s along the road = 90 km/h
    v, s = cal.speed(t, pts)
    assert abs(v - 90.0) < 0.5 and 0.02 * v < s < 0.15 * v
    run = lambda lim: [e for e in _drive(_monitor(cal=True, speed_limit_kmh=lim),   # noqa: E731
                                         {1: lambda f: (550, 200 + 500 / 30 * f)}, 45) if e.code == SPEEDING]
    fast = run(60.0)
    assert len(fast) == 1 and abs(fast[0].value - 90) < 2 and fast[0].error > 0
    assert run(88.0) == []                                              # 90 km/h, but not beyond doubt
    assert [e for e in _drive(_monitor(cal=False, speed_limit_kmh=60.0), {1: lambda f: (550, 200 + 500 / 30 * f)},
                              45) if e.code == SPEEDING] == []          # no calibration, no km/h


def test_tailgating_from_the_headway_at_the_count_line():
    from alpr.behaviour import TAILGATING
    def run(gap_frames, cal=True):
        mon = _monitor(cal=cal, count_line_y=0.6, tailgate_headway_sec=1.0, tailgate_min_kmh=50.0)
        lead = lambda f: (550, 200 + 500 / 30 * f)                      # 25 m/s, down A1    # noqa: E731
        foll = lambda f: (550, 200 + 500 / 30 * (f - gap_frames)) if f > gap_frames else None   # noqa: E731
        return [e for e in _drive(mon, {1: lead, 2: foll}, 60 + gap_frames) if e.code == TAILGATING]
    close = run(15)                                                     # 0.5 s behind
    assert len(close) == 1 and close[0].track_id == 2 and abs(close[0].value - 0.5) < 0.05
    assert run(75) == []                                                # 2.5 s behind
    assert run(15, cal=False) == []
    assert run(2) == []                                                 # 0.07 s = 1.7 m: beside, not behind
    mon = _monitor(cal=True, count_line_y=0.6, tailgate_headway_sec=1.0, tailgate_min_kmh=50.0)
    lead = (lambda f: (550, 200 + 500 / 30 * f), "Motorcycle")         # a motorcycle is not a leader
    foll = lambda f: (550, 200 + 500 / 30 * (f - 15)) if f > 15 else None   # noqa: E731
    assert [e for e in _drive(mon, {1: lead, 2: foll}, 75) if e.code == TAILGATING] == []


def test_public_footage_withholds_violations_of_camera_vehicles():
    from alpr.behaviour import WRONG_WAY
    up = lambda f: (550, 900 - 6 * f)                                   # noqa: E731
    mon = _monitor()
    ev = _drive(mon, {1: up, -1: (up, "Car", True)}, 90)
    real, sim = (next(e for e in ev if e.track_id == t and e.code == WRONG_WAY) for t in (1, -1))
    assert real.withheld and not sim.withheld and sim.simulated
    assert mon.visible_events() == [sim] and mon.withheld_counts() == {WRONG_WAY: 1}
    mon = _monitor(show_real_violations=True)
    assert all(not e.withheld for e in _drive(mon, {1: up}, 90))


def test_per_lane_counts_occupancy_density_and_time_series():
    from alpr.flow import LaneMetrics
    lc = LineCounter(0.5)
    for tid, (f, lane) in enumerate([(30, "A1"), (60, "A1"), (90, "A2")]):
        lc.update(tid, (0, 0, 1, 400), "Car", f - 1, 1000, lane=lane)
        lc.update(tid, (0, 0, 1, 600), "Car", f, 1000, lane=lane)
    assert lc.by_lane() == {"A1": 2, "A2": 1}
    assert abs(lc.rate_per_min(300, 30, lane="A1") - 2 * 60 / 15) < 1e-9
    lm = LaneMetrics(["A1", "A2"], 30.0, {"A1": 50.0})
    for f in range(1, 301):                                             # 10 s: A1 covered 1/4 of the time
        lm.update(f, {"A1": f % 4 == 0}, {"A1": 2, "A2": 1})
    assert abs(lm.occupancy("A1") - 0.25) < 0.01 and lm.occupancy("A2") == 0
    assert lm.density("A1") == (40.0, "veh/km") and lm.density("A2") == (1.0, "veh")
    ts = lm.time_series(lc, bin_sec=5.0, speeds={0: 60.0, 1: 80.0})
    assert [r["count"] for r in ts["A1"]] == [2, 0, 0] and ts["A1"][0]["mean_speed_kmh"] == 70.0
    assert ts["A1"][0]["density_veh_km"] == 40.0 and "density_veh_km" not in ts["A2"][0]


def test_lane_eval_scores_per_lane_and_separates_wrong_lanes():
    from alpr.lane_eval import evaluate_lanes
    gt = [(1.0, "A1"), (2.0, "A1"), (3.0, "A2"), (5.0, "A2")]
    pred = [(1.2, "A1"), (2.1, "A2"), (3.1, "A2"), (9.0, "A1")]
    r = evaluate_lanes(pred, gt, ["A1", "A2"], tol=0.5)
    assert r["per_lane"]["A1"]["matched"] == 1 and r["per_lane"]["A2"]["matched"] == 1
    assert r["wrong_lane"] == 1 and r["missed"] == 1 and r["extra"] == 1
    assert r["per_lane"]["A1"]["count_accuracy"] == 1.0                # 2 vs 2, though not the same ones
    assert abs(r["per_lane"]["A2"]["count_accuracy"] - 1.0) < 1e-9 and r["per_lane"]["A2"]["recall"] == 0.5


def test_simulated_traffic_follows_its_path_and_is_marked():
    from alpr.behaviour import SimulatedTraffic
    from alpr.lanes import Calibration
    spec = [{"plate": "XWW 1001", "vtype": "Car", "start_sec": 2.0, "path_img": [[0, 0.55, 0.9, 0.06], [2, 0.55, 0.3, 0.04]]}]
    st = SimulatedTraffic(spec, None, 1000, 1000, 30.0)
    assert st.vehicles(30) == []                                        # before start_sec
    v0, v1 = st.vehicles(61)[0], st.vehicles(90)[0]
    assert v0["simulated"] and v0["track_id"] == -1 and v0["plate"] == "XWW 1001"
    assert v1["box"][3] < v0["box"][3] and abs((v1["box"][0] + v1["box"][2]) / 2 - 550) <= 1
    assert st.vehicles(200) == []
    cal = Calibration.from_config(_CAL, 1000, 1000)
    st = SimulatedTraffic([{"plate": "X1", "start_sec": 0, "path": [[0, 1.75, 30.0], [3, 1.75, 0.0]]}], cal, 1000, 1000, 30.0)
    b = st.vehicles(45)[0]["box"]                                       # 1.5 s: 15 m, front edge at 12.75 m
    assert abs((b[0] + b[2]) / 2 - 550) < 2 and abs(b[3] - (1000 - 20 * 12.75)) < 2


def test_dashboard_and_summary_render_lanes_and_events():
    from alpr.behaviour import LaneEvent
    from alpr.hud import render_dashboard, render_summary
    lm = _lane_map()
    rows = [{"name": l.name, "group": l.group, "direction": l.direction, "polygon": l.polygon, "count": 3,
             "rate": 12.0, "veh_per_min": 12.0, "occupancy": 0.5 if l.name == "B1" else 0.1, "density": 30.0,
             "density_unit": "veh/km", "speed": 60.0} for l in lm.lanes]
    evs = [LaneEvent("LANE_CHANGE", 1, 10, "A2", "Lane change A1 → A2", from_lane="A1", vtype="Car", plate="WA 1234 B"),
           LaneEvent("WRONG_WAY", -1, 20, "A1", "Moving against the traffic", vtype="Car", simulated=True, plate="XWW 1001")]
    lanes = {"map": lm, "rows": rows, "events": evs, "withheld": {"TAILGATING": 2}, "calibrated": True,
             "simulated": True, "fps": 30.0, "t0": 0.0}
    cfg = AlprConfig(hud_mode="dashboard", count_line_y=0.6, hud_banner="Simulation · test")
    vehicles = [{"track_id": -1, "box": [520, 500, 580, 560], "vtype": "Car", "simulated": True, "plate_box": None,
                 "result": PlateResult("XWW 1001", "XWW1001", 1.0, 0, CONFIRMED), "alerts": [], "events": [evs[1]]}]
    stats = {"fps": 5.0, "fps_video": 30.0, "time_sec": 1.0, "count": 9, "by_type": {"Car": 9}, "rate_per_min": 30.0,
             "plates": 0, "confirmed": 0, "alerts": None}
    out = render_dashboard(np.full((1000, 1000, 3), 90, np.uint8), vehicles, [], stats, cfg, lanes=lanes)
    assert out.shape == (1080, 1920, 3)
    b, g, r = (out[..., k].astype(int) for k in range(3))
    purple = (r > 150) & (b > 190) & (g < 130)                          # simulated vehicle (system purple)
    red = (r > 200) & (g < 110) & (b < 110)                             # wrong-way pill / queue lane
    assert purple[:, :1444].any() and red[:, :1444].any() and red[:, 1468:].any()
    card = render_summary([], stats, cfg, lanes=lanes)
    assert card.shape == (1080, 1920, 3) and not np.array_equal(card, render_summary([], stats, cfg))


def test_pipeline_lane_frame_counts_per_lane_and_reports():
    from alpr.flow import LineCounter as LC
    from alpr.pipeline import AlprPipeline
    p = _bare_pipeline(lanes=_LANES, solid_lines=_SOLID, calibration=_CAL, count_line_y=0.6,
                       behaviour_sim_tracks=[{"plate": "XWW 1001", "start_sec": 0.5,
                                              "path_img": [[0, 0.55, 0.3, 0.05], [1, 0.55, 0.9, 0.05]]}])
    p.counter = LC(0.6)
    p._init_lanes(1000, 1000)
    for f in range(1, 61):
        v = {"track_id": 7, "box": _box(650, 300 + 8 * f), "vtype": "Car"}
        sims = p._lanes_frame([v], f, 1000)
        raw = p.lane_map.lane_at(p.lane_map.ref_point(v["box"]))
        p.counter.update(7, v["box"], "Car", f, 1000, lane=v.get("lane") or (raw.name if raw else None))
        if 20 <= f <= 40:
            assert sims and sims[0]["simulated"] and sims[0]["result"].text == "XWW 1001"
    rep = p.lane_report()
    assert rep["per_lane"]["A2"]["count"] == 1 and rep["crossings"][0]["lane"] == "A2"
    assert abs(rep["per_lane"]["A2"]["zone_length_m"] - 40.0) < 0.2 and rep["per_lane"]["A2"]["occupancy"] > 0
    assert set(rep["per_10s"]) == {"A1", "A2", "B1"} and p.counter.total == 1      # simulated car not counted


def test_lane_events_link_to_the_plate_once_it_is_read():
    from alpr.behaviour import STOPPED
    from alpr.pipeline import LogEntry
    p = _bare_pipeline(lanes=_LANES, count_line_y=0.6, stopped_sec=2.0)
    p._init_lanes(1000, 1000)
    for f in range(1, 121):                                             # stands still in A1, nobody else
        p._lanes_frame([{"track_id": 7, "box": _box(550, 600), "vtype": "Car"}], f, 1000)
    ev, = p.lane_events()
    assert ev.code == STOPPED and ev.plate == "" and ev.emitted > ev.frame   # fired 2 s after it began
    res = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
    p.log.append(LogEntry(res, "Car", "", 1, 120, {7}))                  # plate read afterwards
    assert p.lane_events()[0].plate == "WA 1234 B"


# ---------------------------------------------------------------- forecasting (alpr/forecast.py, alpr/series.py)
def _day(n=240, seed=0, start_min=300, peak=150):
    """Synthetic camera-day: occupancy and flow rising to a morning peak around minute `peak`."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    occ = np.clip(0.05 + 0.45 / (1 + np.exp(-(t - peak) / 12.0)) + rng.normal(0, 0.02, n), 0, 1)
    flow = np.clip(5 + 25 * np.exp(-((t - peak + 20) / 40.0) ** 2) + rng.normal(0, 1.5, n), 0, None)
    return t + start_min, occ, flow


def test_forecast_features_use_only_the_past():
    from alpr.forecast import past_features
    mod, occ, flow = _day()
    X, names = past_features(occ, mod, {"flow": flow})
    for t in (40, 120, 200):
        occ2, flow2 = occ.copy(), flow.copy()
        occ2[t + 1:] = 9.0                                              # rewrite the future
        flow2[t + 1:] = -5.0
        X2, _ = past_features(occ2, mod, {"flow": flow2})
        assert np.array_equal(X[:t + 1], X2[:t + 1], equal_nan=True), t
        assert not np.array_equal(X[t + 1:], X2[t + 1:], equal_nan=True)
    assert "y_lag0" in names and "flow_mean15" in names and "tod_sin" in names


def test_forecasts_made_at_t_do_not_change_when_the_future_changes():
    from alpr.forecast import (ARIMAForecaster, DailyProfile, GRUForecaster, LightGBMForecaster, MovingAverage,
                               Persistence, ProfileChange, Target)
    mod_a, occ_a, flow_a = _day(seed=1)
    mod_b, occ_b, flow_b = _day(seed=2, peak=140)
    train = Target(occ_a, mod_a, {"flow": flow_a})
    test = Target(occ_b, mod_b, {"flow": flow_b})
    t = 150
    occ_f, flow_f = occ_b.copy(), flow_b.copy()
    occ_f[t + 1:] = 0.99
    flow_f[t + 1:] = 0.0
    test_f = Target(occ_f, mod_b, {"flow": flow_f})
    models = [Persistence(), MovingAverage(15), DailyProfile(15), ProfileChange(15), ARIMAForecaster(orders=((2, 0, 0), (1, 0, 1))),
              LightGBMForecaster(["flow"]), GRUForecaster(["flow"], seq=30, hidden=8, epochs=20)]
    for m in models:
        m.fit(train) if not isinstance(m, (LightGBMForecaster, GRUForecaster)) else m.fit(train, (5, 15))
        a, b = m.forecast(test, (5, 15)), m.forecast(test_f, (5, 15))
        for h in (5, 15):
            assert np.allclose(a[h][:t + 1], b[h][:t + 1], equal_nan=True, atol=1e-9), (m.name, h)


def test_forecast_metrics_and_congestion_warning_time():
    from alpr.forecast import congestion_scores, congestion_state, mae_rmse, onsets
    mae, rmse, n = mae_rmse(np.array([1.0, 2.0, np.nan, 4.0]), np.array([2.0, 2.0, 5.0, 1.0]))
    assert n == 3 and abs(mae - 4 / 3) < 1e-9 and abs(rmse - np.sqrt(10 / 3)) < 1e-9
    occ = np.r_[np.full(100, 0.1), np.full(60, 0.6), np.full(40, 0.1)]
    st = congestion_state(occ, 0.3, k=5)
    assert onsets(st) == [101] and st[:101].sum() == 0                  # 5-min mean reaches 0.3 at 101 (3 x 0.1 + 2 x 0.6)
    assert onsets(np.r_[np.zeros(10), np.ones(5), np.zeros(10)].astype(int)) == []     # too short to count
    h = 15
    perfect = np.r_[occ[h:], np.full(h, np.nan)]                       # forecast[t] = occ[t + h]
    s = congestion_scores(occ, perfect, h, 0.3)
    assert s["onsets"][0]["warning_min"] >= h - 2 and s["recall"] > 0.9 and s["precision"] > 0.9
    late = congestion_scores(occ, occ.copy(), h, 0.3)                  # persistence: warns only once it has happened
    assert late["onsets"][0]["warning_min"] <= 2


def test_series_builds_minutes_on_the_camera_clock():
    from datetime import datetime
    from alpr.series import minute_series
    per = {"group": "To CIQ", "direction": "up"}
    bins = lambda occ: [{"t_start": 10.0 * i, "t_end": 10.0 * (i + 1), "count": 0, "occupancy": occ,
                         "mean_vehicles": 2.0} for i in range(12)]            # 120 s of video
    rep = {"source": "x.mp4", "lanes": {"lanes": ["T1", "T2"], "per_lane": {"T1": per, "T2": per},
                                        "crossings": [{"time_sec": 5.0, "lane": "T1"}, {"time_sec": 50.0, "lane": "T2"},
                                                      {"time_sec": 70.0, "lane": "T1"}, {"time_sec": 75.0, "lane": ""}],
                                        "per_10s": {"T1": bins(0.2), "T2": bins(0.4)}}}
    s = minute_series([rep], datetime(2023, 3, 22, 7, 0, 0), osd_offset=-0.0, starts=[30.0])  # starts at 07:00:30
    assert [m.strftime("%H:%M") for m in s["minutes"]] == ["07:00", "07:01", "07:02"]
    assert s["rows"]["T1"]["observed_sec"] == [30.0, 60.0, 30.0]
    assert s["rows"]["T1"]["flow"] == [2.0, 1.0, 0.0]                  # 1 car in 30 s = 2 veh/min; 07:01:40 -> 07:01
    assert s["rows"]["To CIQ"]["flow"][1] == 2.0                       # T1 07:01:40 + T2 07:01:20
    assert abs(s["rows"]["To CIQ"]["occupancy"][0] - 0.3) < 1e-9 and s["rows"]["To CIQ"]["density"][0] == 4.0


def test_forecast_panel_renders_and_states_the_outlook():
    from alpr.hud import FORECAST_RGB, forecast_status, render_dashboard
    hist = [(m, 0.1 + 0.003 * (m + 60)) for m in range(-60, 1)]              # rising, 0.28 now
    fc = {"title": "To CIQ", "measure": "occupancy", "model": "LightGBM", "threshold": 0.3, "history": hist,
          "past_forecast": hist[15:], "lag": 15, "forecast": {5: 0.33, 15: 0.38, 30: 0.42}, "error": {5: 0.02, 15: 0.04, 30: 0.06}}
    assert forecast_status(fc)[0] == "Congestion in ~5 min"
    assert forecast_status(dict(fc, forecast={5: 0.2, 15: 0.25, 30: 0.28}))[0].startswith("Free flow")
    assert forecast_status(dict(fc, history=[(0, 0.5)] * 5))[0] == "Congested now"
    assert forecast_status(dict(fc, forecast={}))[0] == "Collecting history"
    lm = _lane_map()
    rows = [{"name": l.name, "group": l.group, "direction": l.direction, "polygon": l.polygon, "count": 2, "rate": None,
             "occupancy": 0.2, "density": 2.0, "density_unit": "veh", "speed": None} for l in lm.lanes]
    lanes = {"map": lm, "rows": rows, "events": [], "withheld": {}, "calibrated": False, "simulated": False, "fps": 30.0,
             "t0": 0.0, "forecast": [fc]}
    cfg = AlprConfig(hud_mode="dashboard", count_line_y=0.6, lane_rules=[], read_plates=False)
    stats = {"fps": 0.0, "fps_video": 30.0, "time_sec": 0.0, "count": 0, "by_type": {}, "rate_per_min": 0.0, "plates": 0,
             "confirmed": 0, "alerts": None, "legend": "congestion", "kpis": [("Camera time", "07:45:00", (255, 255, 255))]}
    out = render_dashboard(np.full((1000, 1000, 3), 90, np.uint8), [], [], stats, cfg, lanes=lanes)
    without = render_dashboard(np.full((1000, 1000, 3), 90, np.uint8), [], [], stats, cfg, lanes=dict(lanes, forecast=[]))
    side = (slice(330, 1080), slice(1468, 1888))                         # sidebar below the lanes table
    b, g, r = (out[side][..., k].astype(int) for k in range(3))
    blue = (abs(r - FORECAST_RGB[0]) < 40) & (abs(g - FORECAST_RGB[1]) < 40) & (abs(b - FORECAST_RGB[2]) < 40)
    assert blue.sum() > 200 and not np.array_equal(out[side], without[side])  # forecast lines drawn only with a forecast


def test_zone_meter_texture_occupancy_and_calibration():
    from alpr.congestion import ZoneMeter, fit_calibration, minute_series, vehicles
    from alpr.lanes import LaneMap
    lanes = [{"name": "T1", "group": "To", "polygon": [[.1, .4], [.5, .4], [.5, .6], [.1, .6]]},
             {"name": "T2", "group": "To", "polygon": [[.5, .4], [.9, .4], [.9, .6], [.5, .6]]}]
    lm = LaneMap.from_config(lanes, [], 400, 200)
    meter = ZoneMeter(lm, 400, 200, {"night_sky": 10})
    road = np.full((200, 400, 3), 110, np.uint8)
    cv2.line(road, (40, 100), (360, 100), (230, 230, 230), 2)          # a lane marking: in every frame
    light, occ = meter.measure(road)
    assert light == "day" and 0.0 < occ["T1"] < 0.2 and abs(occ["T1"] - occ["T2"]) < 0.01   # the marking: a constant
    car = road.copy()
    for x in (60, 120):                                                  # two "vehicles" in T1 (roof, windows)
        cv2.rectangle(car, (x, 84), (x + 40, 96), (30, 30, 200), -1)
        cv2.rectangle(car, (x + 8, 87), (x + 18, 93), (220, 220, 220), -1)
    _, occ2 = meter.measure(car)
    assert occ2["T1"] > occ["T1"] + 0.05 and occ2["T2"] == occ["T2"]
    assert meter.edge_mask(car).sum() > meter.edge_mask(road).sum()
    # vehicles = 20 x occupancy + 1 by day, 2 vehicles fewer at night (other intercept); T2 = 15 x occ - 0.5
    pairs = [("T1", "07:00", "day", 0.1, 3), ("T1", "08:00", "day", 0.2, 5), ("T1", "09:00", "day", 0.4, 9),
             ("T1", "03:00", "night", 0.15, 2), ("T1", "04:00", "night", 0.25, 4),
             ("T2", "07:00", "day", 0.1, 1), ("T2", "08:00", "day", 0.3, 4), ("T2", "09:00", "day", 0.5, 7),
             ("T2", "03:00", "night", 0.2, 2.5), ("T2", "04:00", "night", 0.3, 4), ("T3", "07:00", "day", 0.1, 1)]
    cal = fit_calibration(pairs, {"To": ["T1", "T2"]})
    t1 = cal["zones"]["T1"]
    assert abs(t1["slope"] - 20.0) < 1e-6 and abs(t1["intercept"] - 1.0) < 1e-6 and abs(t1["night_offset"] + 2.0) < 1e-6
    assert t1["loo_mae"] < 1e-6 and "T3" not in cal["zones"]           # one count: no fit
    assert abs(vehicles(cal, "T1", 0.3, "night") - 5.0) < 1e-6 and vehicles(cal, "T3", 0.3) is None
    assert cal["groups"]["To"]["frames"] == 5 and cal["groups"]["To"]["loo_mae"] < 1e-6
    res = {"zones": ["T1", "T2"], "groups": {"To": ["T1", "T2"]}, "params": {"jam_delta": 0.15},
           "samples": [{"time": "2023-03-22T03:30:00", "lighting": "night", "zones": {"T1": 0.1, "T2": 0.05}},
                       {"time": "2023-03-22T07:00:05", "lighting": "day", "zones": {"T1": 0.5, "T2": 0.1}},
                       {"time": "2023-03-22T07:00:40", "lighting": "day", "zones": {"T1": 0.7, "T2": 0.5}},
                       {"time": "2023-03-22T07:02:10", "lighting": "day", "zones": {"T1": 0.1, "T2": 0.6}}]}
    s = minute_series(res)                                              # no calibration: excess + queue only
    i = [m.strftime("%H:%M") for m in s["minutes"]].index("07:00")
    assert s["baselines"] == {"T1": 0.1, "T2": 0.05}                        # 03:00-05:00 median = night baseline
    assert s["rows"]["T1"]["excess"][i] == 0.5 and s["rows"]["To"]["queue"][i] == 2.0   # both zones >= 0.15 above
    assert s["rows"]["To"]["queue"][i + 2] == 1.0                           # 07:02: only T2 (0.6 - 0.05)
    assert s["rows"]["To"]["density"][i] != s["rows"]["To"]["density"][i]   # no calibration: no vehicles
    cal2 = {"zones": {"T1": {"slope": 20.0, "intercept": 0.0}, "T2": {"slope": 20.0, "intercept": 0.0}},
            "capacity": {"T1": 16, "T2": 20}}
    s = minute_series(res, cal2)
    assert s["rows"]["T1"]["occupancy"][i] == 0.6 and s["rows"]["To"]["density"][i] == 18.0   # 20 x (0.6 + 0.3)
    assert s["rows"]["T1"]["fill"][i] == 0.75 and s["rows"]["To"]["fill"][i] == 0.5      # 18 / (16 + 20)
    assert s["rows"]["To"]["queue"][i] == 1.0                               # T1 at 0.75 >= 0.5, T2 at 0.3
    assert s["rows"]["To"]["occupancy"][i + 1] != s["rows"]["To"]["occupancy"][i + 1]   # 07:01 not observed: NaN


# ---------------------------------------------------------------- code review fixes (2026-10-22)
def test_cloned_plate_is_flagged_on_both_vehicles_when_a_reading_settles_later():
    """B's plate is first confirmed as a misread; when it settles on A's plate, A has left the view and gets
    no update of its own, yet both are flagged."""
    p, a, b = _bare_pipeline(), _car_track(), _car_track()
    real = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
    boxes = {1: [0, 0, 100, 100], 2: [300, 0, 400, 100]}                 # side by side
    for idx in range(1, 10):
        p._update_log(1, a, real, idx, boxes)
        p._update_log(2, b, PlateResult("WA 1284 B", "WA1284B", 0.90, 6, CONFIRMED), idx, boxes)
    assert [_codes(e.alerts) for e in p.log] == [[], []]
    for idx in range(10, 14):                                           # A out of view
        p._update_log(2, b, PlateResult("WA 1234 B", "WA1234B", 0.97, 20, CONFIRMED), idx, {2: boxes[2]})
    assert [_codes(e.alerts) for e in p.log] == [[DUPLICATE], [DUPLICATE]]


def test_overlapping_plate_boxes_cost_no_ocr_when_nothing_would_be_read():
    class CountingOCR:
        calls = 0

        def read_plate(self, crop):
            CountingOCR.calls += 1
            return None

    p = _bare_pipeline()
    p.ocr, p.voter = CountingOCR(), PlateVoter(min_votes=3)
    cands = [((10, 40, 110, 70), 0.9), ((10, 40, 110, 90), 0.8)]        # the plate, and plate + grille below it
    vbox = (100, 100, 300, 300)
    p._read_plate(np.full((400, 400, 3), 128, np.uint8), {"track_id": 1}, _car_track(), 5, vbox, cands)
    assert CountingOCR.calls == 0                                       # flat crop: below the sharpness gate
    sharp = np.random.default_rng(0).integers(0, 256, (400, 400, 3), dtype=np.uint8)
    p._read_plate(sharp, {"track_id": 1}, _car_track(), 5, vbox, cands)
    assert CountingOCR.calls >= 2                                       # readable: OCR settles which box it is


def test_lane_rate_is_over_the_time_seen_on_a_rotating_view():
    from alpr.flow import Crossing, LaneMetrics
    lc = LineCounter(0.5)
    lc.crossings.append(Crossing(15, 1, "Car", "inbound", "A1"))
    gated = LaneMetrics(["A1"], 10.0)
    for f in list(range(1, 31)) + [99]:                                 # 3.1 s of the 10 s bin seen, the rest skipped
        gated.update(f, {}, {})
    row = gated.time_series(lc, 10.0)["A1"][0]
    assert row["observed_sec"] == 3.1 and row["veh_per_min"] == round(60 / 3.1, 1)
    full = LaneMetrics(["A1"], 10.0)
    for f in range(1, 100):
        full.update(f, {}, {})
    assert full.time_series(lc, 10.0)["A1"][0]["veh_per_min"] == 6.0   # every frame seen: over the whole bin


def test_gru_validates_after_a_gap_and_pools_only_series_on_the_same_scale():
    from alpr.forecast import GRUForecaster, LightGBMForecaster, Target
    g = GRUForecaster(seq=30, hidden=4, epochs=3)
    ends = np.arange(29, 300)
    tr, va = g._split(ends, 30)
    assert va.sum() == int(0.2 * len(ends)) and ends[tr].max() + 30 < ends[va].min()   # no training target inside
    mod, occ, _ = _day(seed=3)
    train = Target(np.r_[np.full(70, np.nan), occ[70:]], mod)          # starts late: a gap longer than seq
    g.fit(train, (5, 15), pool=[Target(np.clip(occ + 0.1, 0, 1), mod)])   # a zone at its own level: same scale
    out = g.forecast(Target(occ, mod), (5, 15))
    assert np.isfinite(out[5][29:]).all() and np.isfinite(out[15][29:]).all()
    for model in (GRUForecaster(seq=30, hidden=4, epochs=1), LightGBMForecaster()):
        try:                                                            # a sum over zones (vehicles) is not
            model.fit(train, (5, 15), pool=[Target(occ * 5.0, mod)])
            raise AssertionError(f"{model.name} pooled a series on another scale")
        except ValueError as e:
            assert "same scale" in str(e)


def test_arima_refuses_a_training_day_without_enough_data():
    from alpr.forecast import ARIMAForecaster, Target
    try:
        ARIMAForecaster().fit(Target(np.r_[np.full(50, np.nan), 1.0, 2.0], np.arange(52)))
    except ValueError as e:
        assert "observed minutes" in str(e)
    else:
        raise AssertionError("ARIMA fitted on 2 minutes")


def test_daily_profile_wraps_past_midnight():
    from alpr.forecast import DailyProfile, ProfileChange, Target
    day = Target(np.arange(120, dtype=float), np.r_[np.arange(1380, 1440), np.arange(0, 60)])   # 23:00 -> 00:59
    assert DailyProfile(smooth=1).fit(day).forecast(day, (30,))[30][50] == 80.0   # 23:50 + 30 min = 00:20
    assert ProfileChange(smooth=1).fit(day).forecast(day, (30,))[30][50] == 80.0


def test_congestion_minutes_use_the_real_keyframe_spacing_and_full_times():
    from alpr.congestion import match_counts, minute_series

    def smp(t, dt=None):
        return dict({"time": t, "lighting": "day", "zones": {"T1": 0.2}}, **({"dt": dt} if dt else {}))

    res = {"zones": ["T1"], "groups": {"To": ["T1"]}, "params": {},
           "samples": [smp(f"2023-03-22T07:00:{s:02d}", 2.0) for s in range(0, 60, 2)]}
    s = minute_series(res)
    assert s["rows"]["T1"]["observed_sec"] == [60.0] and s["rows"]["To"]["observed_sec"] == [60.0]
    res["samples"] = [smp(f"2023-03-22T07:00:{s:02d}", 5.333) for s in (0, 6, 11)]
    assert minute_series(res)["rows"]["T1"]["observed_sec"] == [16.0]
    res["samples"] = [smp("2023-03-22T07:00:00"), smp("2023-03-22T07:00:05")]   # measured before "dt" was kept
    assert minute_series(res)["rows"]["T1"]["observed_sec"] == [10.0]
    two_days = [smp("2023-03-22T07:15:00"), smp("2023-03-23T07:15:00")]
    try:
        match_counts(two_days, [("07:15:00", "T1", 3)])
        raise AssertionError("an HH:MM:SS matching two dates was accepted")
    except ValueError:
        pass
    assert match_counts(two_days, [("2023-03-23T07:15:00", "T1", 3)]) == [("T1", "2023-03-23T07:15:00", "day", 0.2, 3)]


def test_rule_params_take_smoothing_and_speed_window_from_the_config():
    from alpr.behaviour import RuleParams
    p = RuleParams.from_config(AlprConfig(smooth_sec=0.5, speed_window_sec=2.0))
    assert (p.smooth_sec, p.speed_window_sec) == (0.5, 2.0)
    d = RuleParams.from_config(AlprConfig())
    assert (d.smooth_sec, d.speed_window_sec) == (RuleParams().smooth_sec, RuleParams().speed_window_sec)


def test_requirements_cover_every_third_party_import():
    import glob
    import re
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    dist = {"cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "fast_plate_ocr": "fast-plate-ocr",
            "imageio_ffmpeg": "imageio-ffmpeg", "yt_dlp": "yt-dlp"}
    with_ultralytics = {"torch", "torchvision"}
    with open(os.path.join(root, "requirements.txt"), encoding="ascii") as f:   # ASCII only: pip reads it as GBK
        listed = {re.split(r"[<>=!~\[\s]", s)[0].lower() for s in (ln.split("#")[0].strip() for ln in f) if s}
    missing = set()
    for path in glob.glob(os.path.join(root, "alpr", "*.py")) + [os.path.join(root, n) for n in ("run_alpr.py", "run_image.py")]:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"\s*(?:from\s+(\w+)[\w.]*\s+import\s|import\s+(\w+))", ln)
                mod = m and (m.group(1) or m.group(2))
                if (mod and mod not in sys.stdlib_module_names and mod not in ("alpr", "__future__")
                        and mod not in with_ultralytics and dist.get(mod, mod).lower() not in listed):
                    missing.add(f"{mod} ({os.path.basename(path)})")
    assert not missing, f"imported but not in requirements.txt: {sorted(missing)}"


if __name__ == "__main__":
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)}
    failed = 0
    for name, fn in tests.items():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:                                           # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
