"""OCR upload handoff, snapshot bounds, permissions and export regressions."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from webapp.tests import test_web as W
from webapp.tests import test_partial_jobs as partial
from webapp import db
from webapp.services import ocr
from webapp.worker import Worker


def event(**extra):
    return {"track_id": 91, "frame": 15, "time_sec": .5, "raw_text": "XQA1235",
        "voter_input_text": "XQA 1235", "ocr_valid_format": True, "ocr_confidence": .91,
        "crop_quality_score": .8, "accepted_for_vote": True, "reject_reason": None,
        "n_backend_calls": 1, **extra}


class OcrEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.st, self.app = W.fresh()
        self.c = W.conn_of(self.st)
        self.addCleanup(self.c.close)
        self.s = db.row(self.c, "SELECT s.* FROM sightings s JOIN cameras c ON c.id=s.camera_id WHERE c.kind='anpr' LIMIT 1")
        self.sid, self.rid = self.s["id"], self.s["run_id"]
        self.c.execute("UPDATE sightings SET track_ids='[]' WHERE run_id=?", (self.rid,))
        self.c.execute("UPDATE sightings SET track_ids='[91]',first_frame=1,last_frame=60,first_sec=0,last_sec=2 WHERE id=?", (self.sid,))
        self.c.execute("UPDATE runs SET frames=60,window_start_sec=0,window_end_sec=2,complete=1 WHERE id=?", (self.rid,))
        self.path = self.st.data_dir / "read_fixture.jsonl"

    def save(self, events):
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        ocr.import_stream(self.c, self.rid, self.path)

    def read(self, username="police1", **params):
        client = W.client(self.app, username)
        self.addCleanup(client.close)
        return client.get(f"/api/v1/sightings/{self.sid}/ocr", params=params)

    def test_track_and_window_association_and_retry(self):
        self.save([event(), event(track_id=92), event(frame=61,time_sec=2.03), event(time_sec=4)])
        result = self.read().json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["raw_text"], "XQA1235")
        self.assertEqual(result["stream"]["unmatched"], 3)
        ocr.import_stream(self.c,self.rid,self.path)
        self.assertEqual(self.read().json()["total"],1)

    def test_same_plate_in_another_run_does_not_link(self):
        self.save([event(track_id=999)])
        self.assertEqual(self.read().json()["total"],0)
        self.assertEqual(self.read().json()["stream"]["unmatched"],1)

    def test_ambiguous_same_track_is_not_guessed(self):
        other = dict(self.s); other.pop("id")
        other.update(natural_key="ambiguous-fixture",track_ids="[91]",first_frame=1,last_frame=60,first_sec=0,last_sec=2)
        db.insert(self.c,"sightings",other)
        self.save([event()])
        self.assertEqual(self.read().json()["total"],0)

    def test_partial_cutoff_torn_line_and_malformed_event(self):
        self.c.execute("UPDATE runs SET complete=0,frames=30,window_end_sec=1 WHERE id=?",(self.rid,))
        self.path.write_bytes((json.dumps(event())+"\n"+json.dumps(event(frame=45,time_sec=1.5))+"\n{}\n").encode()+b'{"track_id":')
        ocr.import_stream(self.c,self.rid,self.path)
        d=self.read().json()
        self.assertFalse(d["complete"])
        self.assertEqual(d["saved_frames"],30)
        self.assertEqual(d["saved_window"],[0,1])
        self.assertEqual(d["total"],1)
        self.assertEqual(d["stream"]["invalid"],1)
        self.assertEqual(d["stream"]["trailing_line"],1)
        self.assertEqual(d["stream"]["state"],"incomplete")

    def test_missing_old_and_unreadable_logs_are_explicit(self):
        self.assertEqual(self.read().json()["stream"]["state"],"not_recorded")
        ocr.import_stream(self.c,self.rid,self.path)
        self.assertEqual(self.read().json()["stream"]["state"],"missing")
        self.save([event()])
        with patch.object(Path,"open",side_effect=PermissionError):
            ocr.import_stream(self.c,self.rid,self.path)
        self.assertEqual(self.read().json()["stream"]["state"],"unreadable")
        self.assertEqual(self.read().json()["total"],0)

    def test_masked_roles_do_not_receive_raw_alternatives_even_after_reveal(self):
        self.save([event()])
        for name in [n for n,u in W.USERS.items() if u["role"] in ("traffic", "admin")]:
            with self.subTest(user=name):
                client=W.client(self.app,name);self.addCleanup(client.close)
                for reveal in (False,True):
                    if reveal:
                        r=client.post(f"/api/v1/sightings/{self.sid}/reveal",json={"reason_code":"Data-quality check"})
                        self.assertEqual(r.status_code,200)
                    response=client.get(f"/api/v1/sightings/{self.sid}/ocr",params={"export":True})
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn("XQA",response.text)
                    self.assertIsNone(response.json()["items"][0]["raw_text"])
                    self.assertFalse(response.json()["raw_visible"])

    def test_login_camera_scope_and_role_downgrade(self):
        self.save([event()])
        with W.TestClient(self.app) as anon:
            self.assertEqual(anon.get(f"/api/v1/sightings/{self.sid}/ocr").status_code,401)
        self.assertEqual(self.read("guard1").status_code,404)
        police=W.client(self.app,"police1");self.addCleanup(police.close)
        self.assertIn("XQA1235",police.get(f"/api/v1/sightings/{self.sid}/ocr").text)
        self.c.execute("UPDATE users SET role='traffic' WHERE username='police1'")
        self.assertNotIn("XQA",police.get(f"/api/v1/sightings/{self.sid}/ocr",params={"export":True}).text)

    def test_export_pagination_no_store_and_audit(self):
        self.save([event(),event(frame=16,time_sec=.53)])
        r=self.read(export=True,limit=1)
        self.assertEqual(r.status_code,200)
        self.assertIn("attachment",r.headers["content-disposition"])
        self.assertEqual(r.headers["cache-control"],"private, no-store")
        self.assertTrue(r.json()["has_more"])
        self.assertEqual(len(r.json()["items"]),1)
        self.assertEqual(self.read(offset=1,limit=1).json()["items"][0]["frame"],16)
        self.assertTrue(db.scalar(self.c,"SELECT COUNT(*) FROM audit_log WHERE action='ocr_export'"))

    def test_invalid_fields_and_limits_do_not_import_unknown_text(self):
        self.save([event(frame=True),event(ocr_confidence=float("nan")),event(raw_text="x"*257),event(reject_reason="secret plate"),event()])
        d=self.read().json()
        self.assertEqual(d["total"],1)
        self.assertEqual(d["stream"]["invalid"],4)
        with patch.object(ocr,"MAX_LINES",1):
            self.save([event(),event(frame=16,time_sec=.53)])
        self.assertTrue(self.read().json()["stream"]["limited"])

    def test_sighting_retention_cascades_raw_evidence(self):
        self.save([event()])
        self.c.execute("DELETE FROM sightings WHERE id=?",(self.sid,))
        self.assertEqual(db.scalar(self.c,"SELECT COUNT(*) FROM ocr_read_evidence WHERE sighting_id=?",(self.sid,)),0)
        self.assertEqual(self.read().status_code,404)

    def test_rejected_empty_read_and_nonzero_source_offset(self):
        self.c.execute("UPDATE runs SET window_start_sec=10,window_end_sec=12 WHERE id=?",(self.rid,))
        self.c.execute("UPDATE sightings SET first_sec=10,last_sec=12 WHERE id=?",(self.sid,))
        self.save([event(time_sec=10.5,raw_text=None,voter_input_text=None,ocr_valid_format=None,ocr_confidence=None,accepted_for_vote=False,reject_reason="empty_read")])
        d=self.read().json()
        self.assertEqual(d["total"],1)
        self.assertEqual(d["items"][0]["reject_reason"],"empty_read")

    def test_additive_schema_is_idempotent(self):
        self.save([event()]);db.init(self.c);db.init(self.c)
        self.assertEqual(self.read().json()["total"],1)


class WorkerOcrEvidenceTests(unittest.TestCase):
    setUp = partial.PartialJobsTests.setUp
    save = partial.PartialJobsTests.save
    finish = partial.PartialJobsTests.finish
    state = partial.PartialJobsTests.state

    def stream(self):
        self.row.update(track_ids=[91],first_frame=1,last_frame=30,first_sec=.03,last_sec=1)
        (self.folder/"ocr_reads.jsonl").write_text(json.dumps(event())+"\n",encoding="utf-8")

    def count(self):
        return db.scalar(self.c,"SELECT COUNT(*) FROM ocr_read_evidence e JOIN sightings s ON s.id=e.sighting_id WHERE s.run_id=?",(self.state()["run_id"],))

    def test_failed_and_cancelled_jobs_import_saved_readings(self):
        self.stream();self.save();self.finish(7)
        self.assertEqual(self.state()["status"],"failed");self.assertEqual(self.count(),1)
        self.c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?",(self.jid,))
        self.finish(0)
        self.assertEqual(self.state()["status"],"cancelled");self.assertEqual(self.count(),1)

    def test_successful_job_imports_readings_without_changing_complete(self):
        self.stream()
        final={**self.partial,"plates":[self.row],"frames":212};final.pop("complete");final.pop("frames_processed")
        (self.folder/"result.plates.json").write_text(json.dumps(final),encoding="utf-8")
        with patch("webapp.worker.playback_copy",return_value=self.input):
            self.finish(0)
        self.assertEqual(self.state()["status"],"done");self.assertEqual(self.count(),1)
        self.assertEqual(self.state()["result_complete"],1)

    def test_real_p03_writer_handoff(self):
        from types import SimpleNamespace
        from alpr.ocr_events import OcrEventWriter
        self.stream()
        writer=OcrEventWriter(self.folder/"ocr_reads.jsonl")
        writer.write(track_id=91,frame=15,time_sec=.5,
            read=SimpleNamespace(raw="XQA1235",text="XQA 1235",valid=True,conf=.91),
            crop_quality_score=.8,accepted_for_vote=True,reject_reason=None,n_backend_calls=1)
        self.save();self.finish(1)
        self.assertEqual(self.count(),1)

    def test_upload_enables_absolute_per_job_ocr_path(self):
        def run(conn,job,cmd,log_path,*args,**kwargs):
            import yaml
            conf=yaml.safe_load((self.folder/"config.yaml").read_text("utf-8"))
            profile=next(iter(conf["profiles"].values()))
            self.assertEqual(Path(profile["ocr_reads_jsonl"]),(self.folder/"ocr_reads.jsonl").resolve())
            return 1
        with patch.object(self.w,"_run",side_effect=run),patch.object(self.w,"_finish_video"):
            self.w._video(self.c,self.job,self.folder)


if __name__ == "__main__":
    unittest.main()
