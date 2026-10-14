"""Unit tests for the model-free ALPR logic (plates, slot decoding, voting, quality, lead, evaluate,
dataset labels, OCR reading order).

No weights, video or GPU needed:

    python alpr/tests/test_alpr.py
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from alpr.evaluate import edit_distance, evaluate
from alpr.lead import LeadSelector
from alpr.dataset import parse_label_line
from alpr.eval_images import match
from alpr.flow import LineCounter
from alpr.ocr import EasyPlateOCR
from alpr.plates import decode_slots, parse_plate
from alpr.quality import assess, sharpness
from alpr.voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateVoter


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
