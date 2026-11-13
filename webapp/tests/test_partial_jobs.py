"""Web/recognition terminal recovery contract: no model or GPU required."""
import json
import io
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webapp.tests import test_web as W
from webapp import clock, db
from webapp.worker import Worker
from webapp.services import jobs, traffic
from webapp.security import User


class PartialJobsTests(unittest.TestCase):
    def setUp(self):
        self.st, self.app = W.fresh()
        self.c = W.conn_of(self.st)
        self.addCleanup(self.c.close)
        self.st.cancel_timeout_sec = 0.2
        self.st.terminate_timeout_sec = 0.2
        self.w = Worker(self.st)
        self.cam = db.scalar(self.c, "SELECT id FROM cameras WHERE kind='anpr' LIMIT 1")
        self.user = User(**{k: db.row(self.c, "SELECT * FROM users WHERE username='police1'")[k]
                           for k in ('id','username','display_name','role','site_id')})
        self.input = self.st.data_dir / 'source.mp4'
        self.input.write_bytes(b'fixture')
        self.jid = db.insert(self.c, 'jobs', dict(kind='video',status='running',input_path=str(self.input),
            original_name='source.mp4',scene='roadside',render_video=0,camera_id=self.cam,
            recorded_at=clock.now_iso(),created_at=clock.now_iso(),started_at=clock.now_iso()))
        self.job = db.row(self.c, 'SELECT * FROM jobs WHERE id=?',(self.jid,))
        self.folder = self.st.jobs_dir / f'job_{self.jid}'
        self.folder.mkdir(parents=True)
        d=json.loads((W.ROOT/'demo/malaysia_road_anpr_demo_30s.plates.json').read_text('utf-8'))
        self.row=d['plates'][0]
        self.partial=dict(source=str(self.input),profile='fixture',fps=30.0,window_sec=[0,212/30],
                          complete=False,frames_processed=212,plates=[self.row],
                          registry=dict(rules=['ILLEGAL_FORMAT'],disclaimer='SIMULATED fixture'))
        (self.folder/'log.txt').write_text('fixture process error',encoding='utf-8')

    def save(self, data=None):
        (self.folder/'result.partial.json').write_text(json.dumps(data or self.partial),encoding='utf-8')

    def state(self):
        return db.row(self.c,'SELECT * FROM jobs WHERE id=?',(self.jid,))

    def finish(self,rc=0):
        self.w._finish_video(self.c,self.job,self.folder,rc,{'label':'test'})

    def cancel(self):
        return jobs.cancel(self.c,self.user,self.jid)

    def test_cancel_is_pending_and_idempotent_until_exit(self):
        j=self.cancel()
        self.assertEqual(j.status,'running');self.assertIsNone(j.finished_at)
        self.assertTrue(self.cancel().cancel_requested)
        self.save();self.finish(0)
        j=self.state();self.assertEqual(j['status'],'cancelled');self.assertIsNotNone(j['finished_at'])
        self.assertEqual(j['saved_frames'],212);self.assertEqual(j['result_complete'],0)

    def test_failure_recovers_and_keeps_failed(self):
        self.save();self.finish(7)
        j=self.state();self.assertEqual(j['status'],'failed');self.assertIsNotNone(j['run_id'])
        r=db.row(self.c,'SELECT * FROM runs WHERE id=?',(j['run_id'],))
        self.assertEqual(r['frames'],212);self.assertEqual(r['complete'],0);self.assertEqual(r['flow_primary'],0)
        self.assertEqual(json.loads(r['registry_rules']),['ILLEGAL_FORMAT'])
        self.assertEqual(r['registry_disclaimer'],'SIMULATED fixture')
        with self.assertRaisesRegex(ValueError,'unavailable'):
            traffic.summary(self.c,self.user,r['id'])

    def test_partial_excludes_any_unsupported_traffic_blocks(self):
        self.partial['flow']={'total':999,'by_type':{'Car':999}}
        self.save();self.finish(1)
        r=db.row(self.c,'SELECT * FROM runs WHERE id=?',(self.state()['run_id'],))
        self.assertIsNone(r['flow_total'])

    def test_empty_positive_progress_is_valid(self):
        self.partial['plates']=[];self.save();self.finish(1)
        self.assertIsNotNone(self.state()['run_id'])
        self.assertIn('no plate records',self.state()['message'])

    def test_missing_zero_and_malformed_snapshot(self):
        for mode in ('missing','zero','malformed'):
            with self.subTest(mode=mode):
                if mode=='zero':
                    self.partial['frames_processed']=0;self.save()
                elif mode=='malformed':
                    (self.folder/'result.partial.json').write_text('{')
                self.finish(1)
                self.assertIsNone(self.state()['run_id']);self.assertEqual(self.state()['status'],'failed')

    def test_invalid_row_rolls_back_all_imported_data(self):
        self.partial['plates'].append({'broken':True});self.save();self.finish(1)
        self.assertIsNone(self.state()['run_id'])
        self.assertEqual(db.scalar(self.c,"SELECT COUNT(*) FROM runs WHERE code=?",(f'upload-{self.jid}',)),0)

    def test_stale_execution_directory_is_not_imported(self):
        self.save();self.w.run_job(self.c,self.job)
        self.assertEqual(self.state()['status'],'failed');self.assertIsNone(self.state()['run_id'])

    def test_identical_snapshot_retry_has_no_duplicates(self):
        self.save();self.finish(1);rid=self.state()['run_id']
        self.finish(1)
        self.assertEqual(rid,self.state()['run_id'])
        self.assertEqual(db.scalar(self.c,'SELECT COUNT(*) FROM sightings WHERE run_id=?',(rid,)),1)

    def test_evidence_attached_and_timestamp_not_offset_twice(self):
        import cv2
        import numpy as np
        for kind in ('crop','scene'):
            cv2.imwrite(str(self.folder/f'{kind}.jpg'),np.zeros((20,30,3),dtype=np.uint8))
        self.row['evidence']=dict(crop=str(self.folder/'crop.jpg'),scene=str(self.folder/'scene.jpg'),frame=2,time_sec=1.6)
        self.partial.update(fps=5.0,window_sec=[1.2,1.6],frames_processed=2)
        self.row.update(first_sec=1.4,last_sec=1.6)
        self.save();self.finish(1)
        s=db.row(self.c,'SELECT * FROM sightings WHERE run_id=?',(self.state()['run_id'],))
        self.assertTrue((self.st.media_dir/s['crop_path']).exists())
        self.assertTrue((self.st.media_dir/s['scene_path']).exists())
        self.assertEqual(s['seen_at'],clock.iso(clock.parse(self.job['recorded_at'])+__import__('datetime').timedelta(seconds=1.4)))

    def test_missing_evidence_preserves_sighting(self):
        self.row['evidence']={'crop':str(self.folder/'missing.jpg'),'scene':str(self.folder/'missing2.jpg')}
        self.save();self.finish(1)
        self.assertIn('evidence unavailable',self.state()['message'])
        self.assertIsNotNone(self.state()['run_id'])

    def write_evidence(self):
        import cv2
        import numpy as np
        for kind in ('crop', 'scene'):
            self.assertTrue(cv2.imwrite(str(self.folder / f'{kind}.jpg'), np.zeros((20, 30, 3), np.uint8)))
        self.row['evidence'] = {kind: str(self.folder / f'{kind}.jpg') for kind in ('crop', 'scene')}

    def assert_unavailable_evidence(self, reason):
        job = self.state()
        self.assertIsNotNone(job['run_id'])
        sighting = db.row(self.c, 'SELECT * FROM sightings WHERE run_id=?', (job['run_id'],))
        self.assertEqual(sighting['compact'], self.row['compact'])
        self.assertIsNone(sighting['crop_path'])
        self.assertIsNone(sighting['scene_path'])
        self.assertIn(reason, json.loads(sighting['scene_info'])['unavailable_reason'])
        self.assertIn('evidence unavailable for 1 records', job['message'])
        self.assertIn(reason, job['message'])
        return sighting

    def test_evidence_directory_failure_preserves_partial_records(self):
        self.write_evidence()
        self.save()
        (self.st.media_dir / 'runs').write_bytes(b'conflicting file')
        self.finish(1)
        self.assertEqual(self.state()['status'], 'failed')
        self.assertEqual(self.state()['saved_frames'], 212)
        self.assertEqual(self.state()['result_complete'], 0)
        sighting = self.assert_unavailable_evidence('crop evidence storage unavailable')
        # A file in the parent path yields different OSError subclasses on Windows and Linux.
        self.assertRegex(json.loads(sighting['scene_info'])['unavailable_reason'],
                         r'^crop evidence storage unavailable \((FileExistsError|NotADirectoryError)\)$')

    def test_evidence_copy_failure_preserves_cancelled_records(self):
        self.write_evidence()
        self.save()
        self.cancel()
        with patch('webapp.importer.shutil.copy2', side_effect=PermissionError('fixture permission denied')):
            self.finish(0)
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assert_unavailable_evidence('PermissionError')

    def test_corrupt_or_empty_evidence_is_not_attached(self):
        for kind, contents in (('crop', b'not an image'), ('scene', b'not an image'), ('crop', b'')):
            with self.subTest(kind=kind, contents=contents):
                self.write_evidence()
                (self.folder / f'{kind}.jpg').write_bytes(contents)
                self.save()
                self.finish(1)
                sighting = self.assert_unavailable_evidence(f'{kind} image could not be decoded')
                police = W.client(self.app, 'police1')
                traffic_client = W.client(self.app, 'traffic1')
                self.assertEqual(police.get(f"/media/sightings/{sighting['id']}/crop").status_code, 404)
                self.assertEqual(traffic_client.get(f"/media/sightings/{sighting['id']}/scene").status_code, 403)

    def test_evidence_retry_clears_bad_attachments_and_restores_valid_pair(self):
        self.write_evidence()
        self.save()
        self.finish(1)
        run_id = self.state()['run_id']
        (self.folder / 'scene.jpg').write_bytes(b'corrupt after first import')
        self.finish(1)
        self.assert_unavailable_evidence('scene image could not be decoded')
        self.write_evidence()
        self.finish(1)
        self.assertEqual(self.state()['run_id'], run_id)
        rows = db.rows(self.c, 'SELECT * FROM sightings WHERE run_id=?', (run_id,))
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]['crop_path'])
        self.assertIsNotNone(rows[0]['scene_path'])
        self.assertNotIn('unavailable_reason', json.loads(rows[0]['scene_info']))
        self.assertNotIn('evidence unavailable', self.state()['message'])

    def test_evidence_database_error_still_rolls_back_import(self):
        self.write_evidence()
        self.save()
        self.c.execute("CREATE TRIGGER fixture_evidence_error BEFORE UPDATE OF crop_path ON sightings "
                       "BEGIN SELECT RAISE(ABORT, 'fixture evidence database error'); END")
        self.finish(1)
        self.assertIsNone(self.state()['run_id'])
        self.assertIn('fixture evidence database error', self.state()['error'])
        self.assertEqual(db.scalar(self.c, 'SELECT COUNT(*) FROM runs WHERE code=?',
                                   (f'upload-{self.jid}',)), 0)

    def save_final(self):
        self.save()
        final = {**self.partial, 'frames': 900, 'window_sec': [0, 30]}
        final.pop('complete')
        final.pop('frames_processed')
        (self.folder / 'result.plates.json').write_text(json.dumps(final), encoding='utf-8')

    def test_normal_final_reports_unavailable_evidence(self):
        self.write_evidence()
        (self.folder / 'scene.jpg').write_bytes(b'corrupt')
        self.save_final()
        with patch('webapp.worker.playback_copy', return_value=self.input):
            self.finish(0)
        self.assertEqual(self.state()['status'], 'done')
        self.assert_unavailable_evidence('scene image could not be decoded')

    def test_nonconfirmed_row_does_not_create_registry_alert(self):
        self.row.update(status='TENTATIVE',alerts=[dict(code='ILLEGAL_FORMAT',severity='medium',message='stale')])
        self.save();self.finish(1)
        self.assertEqual(db.scalar(self.c,"SELECT COUNT(*) FROM alerts WHERE run_id=? AND source='registry'",(self.state()['run_id'],)),0)

    def test_normal_final_wins_over_partial(self):
        self.save()
        final={**self.partial,'frames':900,'window_sec':[0,30]};final.pop('complete');final.pop('frames_processed')
        (self.folder/'result.plates.json').write_text(json.dumps(final))
        with patch('webapp.worker.playback_copy',return_value=self.input):self.finish(0)
        self.assertEqual(self.state()['status'],'done');self.assertEqual(self.state()['result_complete'],1)
        with self.assertRaises(ValueError):self.cancel()

    def test_accepted_cancel_wins_even_with_final_file(self):
        self.save();(self.folder/'result.plates.json').write_text(json.dumps({**self.partial,'frames':900}))
        self.cancel();self.finish(0)
        self.assertEqual(self.state()['status'],'cancelled');self.assertEqual(self.state()['saved_frames'],212)

    def test_cancel_racing_final_preparation(self):
        self.save()
        final={**self.partial,'frames':900};final.pop('complete')
        (self.folder/'result.plates.json').write_text(json.dumps(final))
        def prepare(*_):self.cancel();return self.input
        with patch('webapp.worker.playback_copy',side_effect=prepare):self.finish(0)
        self.assertEqual(self.state()['status'],'cancelled')

    def test_silent_child_times_out_without_stdout(self):
        self.cancel();start=time.monotonic()
        rc=self.w._run(self.c,self.job,[sys.executable,'-c','import time;time.sleep(60)'],self.folder/'log.txt')
        self.assertNotEqual(rc,0);self.assertLess(time.monotonic()-start,5)
        self.assertTrue((self.folder/'stop.request').exists())

    def test_cancel_during_playback_reaps_child_and_recovers_partial(self):
        self.save_final()
        self.st.cancel_timeout_sec = 30  # A transcoder cannot honor recognition's stop.request.
        ready = self.folder / 'transcoder.started'
        errors, children = [], []
        done = threading.Event()
        popen = subprocess.Popen
        run = self.w._run

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        def transcode(conn, job, cmd, log_path, **kwargs):
            code = ("from pathlib import Path; import time; "
                    f"Path({str(ready)!r}).touch(); time.sleep(10)")
            return run(conn, job, [sys.executable, '-c', code], log_path, **kwargs)

        def request():
            conn = db.connect(self.st.db_path)
            try:
                deadline = time.monotonic() + 5
                while not done.is_set() and time.monotonic() < deadline:
                    if ready.exists():
                        jobs.cancel(conn, self.user, self.jid)
                        return
                    time.sleep(.01)
                errors.append('Transcoder did not start')
            except Exception as exc:
                errors.append(str(exc))
            finally:
                conn.close()

        monitor = threading.Thread(target=request)
        monitor.start()
        start = time.monotonic()
        try:
            with patch('cv2.VideoCapture') as capture, \
                 patch('imageio_ffmpeg.get_ffmpeg_exe', return_value='fixture-ffmpeg'), \
                 patch('webapp.worker.subprocess.Popen', side_effect=spawn), \
                 patch.object(self.w, '_run', side_effect=transcode):
                capture.return_value.get.return_value = 0
                self.finish(0)
        finally:
            done.set()
            monitor.join(5)
        self.assertFalse(errors, errors)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(self.w._proc)
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertEqual(self.state()['saved_frames'], 212)
        self.assertEqual(self.state()['result_complete'], 0)
        self.assertIsNotNone(self.state()['run_id'])
        self.assertIn('fixture process error', (self.folder / 'log.txt').read_text('utf-8'))

    def test_playback_timeout_reaps_silent_child(self):
        start = time.monotonic()
        rc = self.w._run(self.c, self.job, [sys.executable, '-c', 'import time; time.sleep(10)'],
                         self.folder / 'playback.log', cooperative_stop=False, timeout_sec=.2)
        self.assertNotEqual(rc, 0)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNone(self.w._proc)
        self.assertFalse(self.state()['cancel_requested'])

    def test_playback_timeout_kills_child_that_ignores_terminate(self):
        child = Mock()
        child.stdout = io.StringIO('')
        exited = threading.Event()
        child.poll.side_effect = lambda: -9 if exited.is_set() else None
        child.kill.side_effect = exited.set
        child.wait.return_value = -9
        with patch('webapp.worker.subprocess.Popen', return_value=child):
            rc = self.w._run(self.c, self.job, ['fixture-transcoder'], self.folder / 'playback.log',
                             cooperative_stop=False, timeout_sec=.01)
        self.assertEqual(rc, -9)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        child.wait.assert_called_once()
        self.assertTrue(child.stdout.closed)
        self.assertIsNone(self.w._proc)

    def test_playback_success_or_failure_selects_only_completed_copy(self):
        for rc in (0, 1):
            with self.subTest(returncode=rc):
                self.save_final()
                with patch('cv2.VideoCapture') as capture, \
                     patch('imageio_ffmpeg.get_ffmpeg_exe', return_value='fixture-ffmpeg'), \
                     patch.object(self.w, '_run', return_value=rc) as run:
                    capture.return_value.get.return_value = 0
                    self.finish(0)
                self.assertEqual(self.state()['status'], 'done')
                run.assert_called_once()
                self.assertFalse(run.call_args.kwargs['cooperative_stop'])
                self.assertEqual(run.call_args.kwargs['timeout_sec'], 3600)
                result = db.row(self.c, 'SELECT * FROM runs WHERE id=?', (self.state()['run_id'],))
                expected = self.folder / 'playback.mp4' if rc == 0 else self.input
                self.assertEqual(Path(result['video_path']), expected)

    def test_playback_timeout_with_zero_exit_is_not_a_completed_copy(self):
        child = Mock()
        child.stdout = io.StringIO('')
        exited = threading.Event()
        child.poll.side_effect = lambda: 0 if exited.is_set() else None
        child.terminate.side_effect = exited.set
        child.wait.return_value = 0
        with patch('webapp.worker.subprocess.Popen', return_value=child):
            rc = self.w._run(self.c, self.job, ['fixture-transcoder'], self.folder / 'playback.log',
                             cooperative_stop=False, timeout_sec=.01)
        self.assertNotEqual(rc, 0)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        child.wait.assert_called_once()

    def test_cooperative_zero_exit_still_cancelled(self):
        self.save();self.cancel();self.st.cancel_timeout_sec=5
        code="from pathlib import Path; import time; p=Path("+repr(str(self.folder/'stop.request'))+");\nwhile not p.exists():time.sleep(.01)"
        rc=self.w._run(self.c,self.job,[sys.executable,'-c',code],self.folder/'log.txt')
        self.assertEqual(rc,0);self.finish(rc);self.assertEqual(self.state()['status'],'cancelled')

    def test_queued_cancel_never_claimed(self):
        self.c.execute("UPDATE jobs SET status='queued' WHERE id=?",(self.jid,))
        self.assertEqual(self.cancel().status,'cancelled');self.assertIsNone(self.w._claim(self.c))

    def test_schema_migration_repeatable(self):
        self.c.execute('ALTER TABLE runs DROP COLUMN complete')
        self.c.execute('ALTER TABLE jobs DROP COLUMN saved_frames')
        db.init(self.c);db.init(self.c)
        self.assertIsNone(self.state()['saved_frames'])
        self.assertEqual(db.scalar(self.c,'SELECT MIN(complete) FROM runs'),1)

    def test_job_api_exposes_partial_metadata(self):
        self.save();self.cancel();self.finish()
        j=W.client(self.app,'police1').get(f'/api/v1/jobs/{self.jid}').json()
        self.assertFalse(j['result_complete']);self.assertEqual(j['saved_frames'],212)
        self.assertEqual(j['saved_window'],self.partial['window_sec'])

    def test_recognition_stop_to_web_recovery_integration(self):
        """Actual recognition loop/checkpoint/cleanup, with only inference replaced."""
        import cv2
        import numpy as np
        import threading
        source=self.st.data_dir/'fixture.avi'
        video=cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'MJPG'),10,(64,48))
        self.assertTrue(video.isOpened())
        for _ in range(30):video.write(np.zeros((48,64,3),dtype=np.uint8))
        video.release()
        self.c.execute('UPDATE jobs SET input_path=? WHERE id=?',(str(source),self.jid))
        self.job['input_path']=str(source)
        (self.folder/'log.txt').unlink();self.folder.rmdir()
        self.st.cancel_timeout_sec=10
        class RecognitionFixtureWorker(Worker):
            def _run(me,conn,job,cmd,log_path,total_hint=None):
                cfg=cmd[cmd.index('--config')+1];profile=cmd[cmd.index('--profile')+1]
                return super()._run(conn,job,[sys.executable,str(Path(__file__).resolve()),
                                             '--recognition-fixture',str(cfg),profile],log_path)
        worker=RecognitionFixtureWorker(self.st)
        errors=[]
        done=threading.Event()
        def request():
            conn=db.connect(self.st.db_path)
            try:
                deadline=time.monotonic()+30
                while not done.is_set() and time.monotonic()<deadline:
                    try:d=json.loads((self.folder/'result.partial.json').read_text('utf-8'))
                    except (OSError,ValueError):d={}
                    if d.get('frames_processed',0)>=2:
                        jobs.cancel(conn,self.user,self.jid);return
                    time.sleep(.02)
                if not done.is_set():errors.append('No checkpoint within deadline')
            except Exception as e:errors.append(str(e))
            finally:conn.close()
        monitor=threading.Thread(target=request)
        monitor.start()
        try:worker.run_job(self.c,self.job)
        finally:done.set();monitor.join(5)
        self.assertFalse(errors,errors)
        j=self.state();self.assertEqual(j['status'],'cancelled');self.assertFalse(j['result_complete'])
        self.assertGreaterEqual(j['saved_frames'],2)
        self.assertFalse((self.folder/'result.plates.json').exists())
        s=db.row(self.c,'SELECT * FROM sightings WHERE run_id=?',(j['run_id'],))
        self.assertIsNotNone(s);self.assertTrue((self.st.media_dir/s['crop_path']).exists())
        self.assertTrue((self.st.media_dir/s['scene_path']).exists())
        # Preserve role-scoped access to the attached evidence.
        police=W.client(self.app,'police1');traffic_client=W.client(self.app,'traffic1')
        self.assertEqual(police.get(f"/media/sightings/{s['id']}/scene").status_code,200)
        self.assertEqual(traffic_client.get(f"/media/sightings/{s['id']}/scene").status_code,403)


def recognition_fixture(cfg_path,profile):
    from alpr import pipeline
    from alpr.config import load_config
    from alpr.tests.test_checkpoints import _initialize, _process_frame
    cfg=load_config(profile,cfg_path);cfg.headless=True
    def process(self,frame,idx):
        time.sleep(.05)
        return _process_frame(self,frame,idx)
    with patch.object(pipeline.AlprPipeline,'__init__',_initialize), \
         patch.object(pipeline.AlprPipeline,'process_frame',process), \
         patch.object(pipeline.AlprPipeline,'lane_report',return_value=None), \
         patch.object(pipeline,'PARTIAL_SAVE_INTERVAL_SEC',0):
        return pipeline.run(cfg)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--recognition-fixture':
        sys.exit(recognition_fixture(*sys.argv[2:]))
    unittest.main()
