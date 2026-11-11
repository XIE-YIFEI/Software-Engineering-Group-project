"""P03 alert contract tests: pin the `alerts` payload of Section 5 ("Alerts: fields and semantics", proposed
by Hanwen on 10 Nov 2026) of the Track/OCR data contract v1 (docs/p03_track_ocr_v1_draft.md) as the
recognition module writes it today. v1 changes no payload, so these tests describe the current code.

No models, no video, no network:

    python -m unittest discover -s alpr/tests -p test_alert_contract.py -v

Public repository: every vehicle flagged in this file is fictitious (X-series plate; no Malaysian series
starts with X).
"""
import glob
import itertools
import json
import os
import sys
import unittest
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from alpr.config import AlprConfig
from alpr.pipeline import AlprPipeline, TrackState
from alpr.registry import ALL_RULES, BLACKLISTED, SEVERITY, PlateChecker, Registry, Sighting, VehicleRecord
from alpr.voting import CONFIRMED, PlateResult

# Section 5.2, written out: the six codes in rule order, and the severity fixed per code.
RULE_ORDER = ("UNMATCHED", "EXPIRED", "ILLEGAL_FORMAT", "BLACKLIST", "TYPE_MISMATCH", "DUPLICATE")
SEVERITY_BY_CODE = {"UNMATCHED": "medium", "EXPIRED": "medium", "ILLEGAL_FORMAT": "medium",
                    "BLACKLIST": "high", "TYPE_MISMATCH": "medium", "DUPLICATE": "high"}
ALERT_KEYS = {"code", "severity", "message"}
SIM_EVENT_KEYS = {"plate", "compact", "vtype", "simulated", "time_sec", "alerts"}        # Section 5.4
REGISTRY_KEYS = {"path", "rules", "disclaimer", "alerts", "simulated_alerts"}            # Section 5.4
UPLOAD_RULES = ("ILLEGAL_FORMAT", "DUPLICATE")       # Section 5.3: what the web worker sets for uploads

NOW = datetime(2026, 10, 14, 8, 0)                   # the demo profiles' notional clock
KL, JB = (3.1390, 101.6869), (1.4927, 103.7414)      # ~300 km apart
CAMERA = ("KL camera (test)", *KL)

# One observation per line: every code, alone and combined, a near miss and two texts that are not plates.
OBSERVATIONS = [
    dict(plate="XSC3003", vtype="Car"),                                        # a clean record
    dict(plate="XSA1001", vtype="Car"),                                        # EXPIRED + TYPE_MISMATCH
    dict(plate="XSB2002", vtype="Car"),                                        # BLACKLIST
    dict(plate="XSD4004", vtype="Car"),                                        # DUPLICATE by travel
    dict(plate="XSE5005", vtype="Car"),                                        # UNMATCHED
    dict(plate="XSE5005", vtype="Car", coexisting_tracks=[7]),                 # DUPLICATE in view + UNMATCHED
    dict(plate="XSF6006", vtype="Car", coexisting_tracks=[7]),                 # four alerts at once
    dict(plate="XSF6006", vtype="Car", colour="White", coexisting_tracks=[7, 3]),
    dict(plate="XSF6007", vtype="Car"),                                        # one character off XSF6006
    dict(plate="XSA12345", valid=False, coexisting_tracks=[7]),                # the OCR says: not a plate
    dict(plate="XS123456", coexisting_tracks=[7]),                             # fits no Malaysian layout
]


def _fictitious(plate):
    assert plate.upper().startswith("X"), f"public repository: X-series plates only in test data, not {plate!r}"
    return plate


def _registry():
    """An in-memory registry of fictitious vehicles: expired motorcycle, stolen car, clean car, a car seen
    in Johor Bahru 15 minutes ago, and one that breaks every record rule at once."""
    reg = Registry(":memory:")
    reg.add([
        VehicleRecord("XSA1001", "Motorcycle", "Red", "Honda EX5", date(2026, 3, 31)),
        VehicleRecord("XSB2002", "Sedan", "Silver", "Proton Saga", date(2027, 5, 31), BLACKLISTED,
                      "SIMULATED: reported stolen"),
        VehicleRecord("XSC3003", "Sedan", "White", "Perodua Bezza", date(2027, 1, 31)),
        VehicleRecord("XSD4004", "Hatchback", "White", "Perodua Myvi", date(2027, 1, 31)),
        VehicleRecord("XSF6006", "Motorcycle", "Red", "Yamaha Y15ZR", date(2026, 3, 31), BLACKLISTED,
                      "SIMULATED: reported stolen"),
    ])
    reg.add_sightings([Sighting("XSD4004", datetime(2026, 10, 14, 7, 45), "Johor Bahru (SIM)", *JB)])
    return reg


def _alerts(checker, plate, **observed):
    """`alerts` as the pipeline writes it for one observation: PlateChecker.check through Alert.to_dict."""
    observed.setdefault("when", NOW)
    return [a.to_dict() for a in checker.check(_fictitious(plate), **observed)]


def _codes(alerts):
    return [a["code"] for a in alerts]


def _bare_pipeline(checker, **cfg):
    """An AlprPipeline with only its plate-log and registry state: no models are loaded."""
    p = AlprPipeline.__new__(AlprPipeline)
    p.cfg, p.fps, p.log, p.states = AlprConfig(**cfg), 30.0, [], {}
    p.ocr_events = None
    p.checker, p.clock0 = checker, NOW
    p._reset_apart()
    p._sim_pending = list(p.cfg.registry_sim_events) if checker else []
    return p


def _car(first_frame=1):
    st = TrackState(first_frame, first_frame)
    st.cls_conf[2] = 1.0                             # COCO class 2: Car
    return st


def _confirmed(text):
    return PlateResult(_fictitious(text), text.replace(" ", ""), 0.95, 10, CONFIRMED)


def _committed_reports():
    """(file name, parsed JSON) of every committed recognition result, demo/*.plates.json."""
    reports = []
    for path in sorted(glob.glob(os.path.join(ROOT, "demo", "*.plates.json"))):
        with open(path, encoding="utf-8") as f:
            reports.append((os.path.basename(path), json.load(f)))
    return reports


class _AlertCase(unittest.TestCase):
    def assert_alerts_payload(self, alerts, where=None):
        """Section 5.2: 0 to 4 objects with exactly code / severity / message, unique codes, most severe
        first and then in rule order, ILLEGAL_FORMAT alone."""
        self.assertIsInstance(alerts, list, where)
        self.assertLessEqual(len(alerts), 4, where)
        for a in alerts:
            self.assertEqual(set(a), ALERT_KEYS, where)
            self.assertIn(a["code"], ALL_RULES, where)
            self.assertEqual(a["severity"], SEVERITY[a["code"]], where)
            self.assertIn(a["severity"], ("high", "medium"), where)
            self.assertIsInstance(a["message"], str, where)
            self.assertTrue(a["message"].strip(), where)
        codes = _codes(alerts)
        self.assertEqual(len(codes), len(set(codes)), where)
        self.assertEqual(codes, sorted(codes, key=lambda c: (SEVERITY[c] != "high", ALL_RULES.index(c))), where)
        if "ILLEGAL_FORMAT" in codes:
            self.assertEqual(codes, ["ILLEGAL_FORMAT"], where)


class CheckerAlertTests(_AlertCase):
    """Sections 5.2 and 5.3 on the rules themselves: PlateChecker.check, Alert.to_dict."""

    def setUp(self):
        self.reg = _registry()
        self.addCleanup(self.reg.close)

    def test_codes_and_severities_are_the_documented_ones(self):
        self.assertEqual(tuple(ALL_RULES), RULE_ORDER)
        self.assertEqual(SEVERITY, SEVERITY_BY_CODE)
        with self.assertRaises(ValueError):                      # no seventh code can be switched on
            PlateChecker(self.reg, rules=["STOLEN"])

    def test_payload_shape(self):
        chk = PlateChecker(self.reg, check_colour=True, camera=CAMERA)
        seen = set()
        for obs in OBSERVATIONS:
            alerts = _alerts(chk, **obs)
            self.assert_alerts_payload(alerts, obs)
            self.assertEqual(json.loads(json.dumps(alerts)), alerts, obs)
            seen.update(_codes(alerts))
        self.assertEqual(seen, set(ALL_RULES))                   # every code went through the shape check

    def test_most_severe_first_then_rule_order_at_most_four(self):
        chk = PlateChecker(self.reg, camera=CAMERA)
        four = _alerts(chk, "XSF6006", vtype="Car", coexisting_tracks=[7])
        self.assertEqual(_codes(four), ["BLACKLIST", "DUPLICATE", "EXPIRED", "TYPE_MISMATCH"])
        self.assertEqual([a["severity"] for a in four], ["high", "high", "medium", "medium"])
        self.assertEqual(_codes(_alerts(chk, "XSA1001", vtype="Car")), ["EXPIRED", "TYPE_MISMATCH"])
        # high before medium, although UNMATCHED comes first in rule order
        self.assertEqual(_codes(_alerts(chk, "XSE5005", coexisting_tracks=[7])), ["DUPLICATE", "UNMATCHED"])
        # a twin in view and an impossible trip at once: still one DUPLICATE
        self.assertEqual(_codes(_alerts(chk, "XSD4004", vtype="Car", coexisting_tracks=[7])), ["DUPLICATE"])
        self.assertEqual(_alerts(chk, "XSC3003", vtype="Car"), [])

    def test_illegal_format_is_exclusive(self):
        chk = PlateChecker(self.reg, camera=CAMERA)
        # the OCR's verdict, as the pipeline passes it: five digits, like the malformed plate of the demo
        self.assertEqual(_codes(_alerts(chk, "XSA12345", valid=False, coexisting_tracks=[7])), ["ILLEGAL_FORMAT"])
        # no lookup follows: not even for the plate of a record that breaks every other rule
        self.assertEqual(_codes(_alerts(chk, "XSF6006", valid=False, vtype="Car", coexisting_tracks=[7])),
                         ["ILLEGAL_FORMAT"])
        # without a verdict the text is parsed: six digits fit no Malaysian layout
        self.assertEqual(_codes(_alerts(chk, "XS123456", vtype="Car", coexisting_tracks=[7])), ["ILLEGAL_FORMAT"])
        # rule switched off: a text that is not a plate gets nothing at all
        off = PlateChecker(self.reg, rules=[r for r in ALL_RULES if r != "ILLEGAL_FORMAT"], camera=CAMERA)
        self.assertEqual(_alerts(off, "XSA12345", valid=False, coexisting_tracks=[7]), [])
        self.assertEqual(_alerts(off, "XSF6006", valid=False, vtype="Car", coexisting_tracks=[7]), [])
        self.assertEqual(_alerts(off, "XS123456", vtype="Car", coexisting_tracks=[7]), [])

    def test_one_character_off_a_registered_plate_raises_nothing(self):
        tolerant = PlateChecker(self.reg)                        # registry_misread_tolerance: on by default
        strict = PlateChecker(self.reg, misread_tolerance=False)
        for near in ("XSF6007", "XSF606", "XSF6006A"):           # substituted, dropped, added character
            # no UNMATCHED, and none of the rules of XSF6006's record (blacklisted, expired, a motorcycle)
            self.assertEqual(_alerts(tolerant, near, vtype="Car"), [], near)
            self.assertEqual(_codes(_alerts(strict, near, vtype="Car")), ["UNMATCHED"], near)
        self.assertEqual(_codes(_alerts(tolerant, "XSF6017", vtype="Car")), ["UNMATCHED"])   # two characters off

    def test_registry_rules_limit_the_codes_that_can_appear(self):
        upload = PlateChecker(self.reg, rules=UPLOAD_RULES, camera=CAMERA)
        self.assertEqual(_alerts(upload, "XSF6006", vtype="Car"), [])            # four alerts with every rule on
        self.assertEqual(_codes(_alerts(upload, "XSF6006", vtype="Car", coexisting_tracks=[7])), ["DUPLICATE"])
        self.assertEqual(_alerts(upload, "XSE5005", vtype="Car"), [])            # UNMATCHED is off
        self.assertEqual(_codes(_alerts(upload, "XSA12345", valid=False)), ["ILLEGAL_FORMAT"])
        for n in range(len(ALL_RULES) + 1):                      # every subset of the six rules
            for rules in itertools.combinations(ALL_RULES, n):
                for tolerance in (True, False):
                    chk = PlateChecker(self.reg, rules=rules, check_colour=True, camera=CAMERA,
                                       misread_tolerance=tolerance)
                    for obs in OBSERVATIONS:
                        alerts = _alerts(chk, **obs)
                        self.assert_alerts_payload(alerts, (rules, tolerance, obs))
                        self.assertLessEqual(set(_codes(alerts)), set(rules), (rules, tolerance, obs))


class PipelineAlertTests(_AlertCase):
    """Sections 5.1 and 5.4 on what the pipeline exports: AlprPipeline.summary and simulated_events."""

    def setUp(self):
        self.reg = _registry()
        self.addCleanup(self.reg.close)

    def test_summary_rows_have_alerts_only_when_registry_checks_are_on(self):
        boxes = {1: [0, 0, 100, 100], 2: [300, 0, 400, 100]}
        off = _bare_pipeline(None)                               # registry_enabled: false
        off._update_log(1, _car(), _confirmed("XSB 2002"), 1, boxes)
        unchecked, = off.summary()
        self.assertNotIn("alerts", unchecked)                    # absent: not checked

        on = _bare_pipeline(PlateChecker(self.reg, camera=CAMERA))
        on._update_log(1, _car(1), _confirmed("XSC 3003"), 1, boxes)
        on._update_log(2, _car(2), _confirmed("XSB 2002"), 2, boxes)
        clean, flagged = on.summary()
        self.assertEqual(clean["alerts"], [])                    # []: checked, nothing found
        self.assertEqual(_codes(flagged["alerts"]), ["BLACKLIST"])
        self.assert_alerts_payload(flagged["alerts"])
        self.assertEqual(set(flagged) - set(unchecked), {"alerts"})   # the only key the checks add to a row

    def test_simulated_events_fields(self):
        events = [{"at_sec": 1.0, "plate": "XSB2002", "vtype": "Car"},
                  {"at_sec": 2.0, "plate": "XSA1001", "vtype": "Car"},
                  {"at_sec": 3.0, "plate": "XSE5005", "vtype": "Car"},
                  {"at_sec": 4.0, "plate": "XSD4004", "vtype": "Car"}]
        p = _bare_pipeline(PlateChecker(self.reg, camera=CAMERA), registry_sim_events=events)
        for idx in range(1, 151):                                # 5 s at 30 FPS
            p._inject_sim_events(idx)
        self.assertEqual(p.summary(), [])                        # not camera vehicles: not in plates[]
        sims = p.simulated_events()
        self.assertEqual([(ev["plate"], ev["compact"], ev["vtype"], ev["simulated"], ev["time_sec"]) for ev in sims],
                         [("XSB 2002", "XSB2002", "Car", True, 1.0), ("XSA 1001", "XSA1001", "Car", True, 2.0),
                          ("XSE 5005", "XSE5005", "Car", True, 3.0), ("XSD 4004", "XSD4004", "Car", True, 4.0)])
        self.assertEqual([_codes(ev["alerts"]) for ev in sims],
                         [["BLACKLIST"], ["EXPIRED", "TYPE_MISMATCH"], ["UNMATCHED"], ["DUPLICATE"]])
        for ev in sims:
            self.assertEqual(set(ev), SIM_EVENT_KEYS)
            self.assert_alerts_payload(ev["alerts"], ev["plate"])
        self.assertEqual(json.loads(json.dumps(sims)), sims)


class CommittedResultsTests(_AlertCase):
    """The committed results demo/*.plates.json, which the web system imports."""

    @classmethod
    def setUpClass(cls):
        cls.reports = _committed_reports()

    def test_alert_payload_in_plates_and_simulated_events(self):
        in_plates = in_events = 0
        for name, d in self.reports:
            for row in d["plates"]:
                if "alerts" in row:
                    self.assert_alerts_payload(row["alerts"], (name, row["plate"]))
                    in_plates += len(row["alerts"])
            for ev in d.get("simulated_events", []):
                self.assert_alerts_payload(ev["alerts"], (name, ev["plate"]))
                in_events += len(ev["alerts"])
        self.assertGreater(in_plates, 0, "no committed result has an alert in plates[] any more")
        self.assertGreater(in_events, 0, "no committed result has an alert in simulated_events[] any more")

    def test_simulated_events_fields(self):
        events = [(name, ev) for name, d in self.reports for ev in d.get("simulated_events", [])]
        self.assertTrue(events, "no committed result has simulated events any more")
        for name, ev in events:
            self.assertEqual(set(ev), SIM_EVENT_KEYS, name)
            self.assertIs(ev["simulated"], True, name)
            for key in ("plate", "compact", "vtype"):
                self.assertIsInstance(ev[key], str, (name, key))
            self.assertEqual(ev["compact"], ev["plate"].replace(" ", ""), name)
            self.assertIsInstance(ev["time_sec"], float, name)
            self.assertEqual(ev["time_sec"], round(ev["time_sec"], 2), name)     # as first_sec: two decimals
            self.assertIsInstance(ev["alerts"], list, name)

    def test_alerts_key_and_registry_block_go_together(self):
        checked = 0
        for name, d in self.reports:
            registry = d.get("registry")
            for row in d["plates"]:                              # absent: not checked; []: nothing found
                self.assertEqual("alerts" in row, registry is not None, (name, row["plate"]))
            if registry is None:
                self.assertFalse(d.get("simulated_events"), name)
                continue
            checked += 1
            self.assertEqual(set(registry), REGISTRY_KEYS, name)
            self.assertEqual(registry["rules"], sorted(registry["rules"]), name)
            self.assertLessEqual(set(registry["rules"]), set(ALL_RULES), name)
            lists = [row["alerts"] for row in d["plates"]] + [ev["alerts"] for ev in d.get("simulated_events", [])]
            self.assertLessEqual({a["code"] for alerts in lists for a in alerts}, set(registry["rules"]), name)
        self.assertGreater(checked, 0, "no committed result was produced with registry checks any more")


if __name__ == "__main__":
    unittest.main()
