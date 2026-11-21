import tempfile
import unittest
import time
import threading
from pathlib import Path
from sports_replay import Track,ReplayCoordinator,grouped_sessions,save_json


def segment(name='a.h265',start=0,end=10,wall=1000):
    return dict(file=name,start_s=start,end_s=end,start_ms=start*1000,end_ms=end*1000,
                base_ms=wall*1000,frames=int((end-start)*10),bytes=1000,dir='/record/game')


class TrackTests(unittest.TestCase):
    def test_clock_offset_maps_same_event_to_same_wall_time(self):
        t=Track('a','game','/record/game',[segment(wall=1002)],offset=2)
        self.assertEqual(t.start,1000)
        self.assertAlmostEqual(t.locate(1003)[1],3)

    def test_gap_is_missing_and_never_substitutes_last_segment(self):
        t=Track('a','game','/record/game',[segment(),segment('b',20,30,1020)])
        self.assertIsNone(t.locate(1015))
        self.assertEqual(t.locate(1021)[0]['file'],'b')

    def test_segment_wall_anchor_is_used_instead_of_assuming_contiguous_pts(self):
        t=Track('a','game','/record/game',[segment(),segment('b',10,20,1012)])
        self.assertIsNone(t.locate(1011))
        self.assertEqual(t.locate(1013)[1],11)

    def test_segment_boundary_uses_next_segment(self):
        t=Track('a','game','/record/game',[segment(),segment('b',10,20,1010)])
        self.assertEqual(t.locate(1010)[0]['file'],'b')

    def test_clock_drift_samples_interpolate(self):
        t=Track('a','game','/record/game',[segment(wall=1002)],clock_samples=[
            dict(remote=1002,offset=2),dict(remote=1012,offset=2.1)])
        self.assertAlmostEqual(t.correction(1007),2.05)
        self.assertAlmostEqual(t.end,1009.9)

    def test_offset_change_invalidates_index(self):
        t=Track('a','game','/record/game',[segment()]);self.assertEqual(t.start,1000)
        t.offset=1;self.assertEqual(t.start,999)

    def test_ring_overlap_extends_past_last_sealed_segment(self):
        t=Track('a','game','/record/game',[segment('ring',0,30,1000),segment('sealed',10,20,1010)])
        self.assertEqual(t.locate(1025)[0]['file'],'ring')

    def test_late_short_recording_cannot_displace_complete_match(self):
        catalogs={}
        for k in ['a','b','c','d']:
            catalogs[k]=[dict(name='long',dir='/record/long',segments=[segment(end=3900)])]
        catalogs['b'].insert(0,dict(name='short',dir='/record/short',segments=[segment(end=6,wall=5000)]))
        group=grouped_sessions(catalogs)[0]
        self.assertEqual(len(group),4)
        self.assertEqual(group['b'].name,'long')


class Loader:
    def __init__(self):self.clips={};self.errors={};self.requests=[]
    def clip(self,name):return self.clips.get(name)
    def error(self,name):return self.errors.get(name)
    def request(self,seg,idle_only=False):self.requests.append(seg)


class Capture:
    def __init__(self):self.released=False
    def read(self):return True,object()
    def set(self,prop,value):return True
    def release(self):self.released=True


class Controller:
    def __init__(self):
        self.opened=None;self.loader=Loader();self.cap=None;self.seg=None;self.drawn=None
        self.mode='live';self.paused=False;self.idx=0;self.fps=10
    def close_session(self,quiet=False):self.opened=None
    def to_live(self):self.mode='live';self.opened=None
    def _open_seg(self,seg,info,pos):
        self.seg=seg;self.cap=Capture();self.idx=round((pos-seg['start_s'])*10)+1
        self.drawn=object();self.pos_s=pos
    def _fit(self,frame):return frame
    def _jump(self,pos):self.idx=round((pos-self.seg['start_s'])*10)+1;self.pos_s=pos
    def _open_capture(self,info):return Capture()
    def _next_seg(self,seg):
        return next((s for s in self.opened['segs'] if s['start_s']>=seg['end_s']),None)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.time=0.;self.ctls={k:Controller() for k in ['a','b']}
        self.co=ReplayCoordinator(self.ctls,lambda:self.time)
        self.co.set_tracks({k:Track(k,'game','/record/game',[segment()]) for k in self.ctls})

    def ready(self):
        for c in self.ctls.values():c.loader.clips['a.h265']={'mkv':'a','fps':10,'frames':100}

    def test_loading_holds_clock_until_all_angles_ready(self):
        self.co.seek(2);self.time=5
        self.ctls['a'].loader.clips['a.h265']={1:2}
        self.co.tick();self.assertEqual(self.co.state,'loading');self.assertEqual(self.co.playhead(),2)
        self.ready();self.co.tick();self.assertEqual(self.co.state,'playing')
        self.time=7;self.assertEqual(self.co.playhead(),4)

    def test_one_clock_drives_both_angles(self):
        self.ready();self.co.seek(1);self.co.tick();self.time=2;self.co.tick()
        self.assertEqual(self.ctls['a'].idx,self.ctls['b'].idx)
        self.assertEqual(self.co.playhead(),3)

    def test_pause_and_quarter_speed_use_common_clock(self):
        self.ready();self.co.seek(1);self.co.tick();self.time=2
        self.co.pause();self.time=5;self.assertEqual(self.co.playhead(),3)
        self.co.set_speed(.25);self.co.pause();self.time=9
        self.assertEqual(self.co.playhead(),4)

    def test_failed_angle_does_not_restart_loading_or_block_other_angle(self):
        self.ready();del self.ctls['b'].loader.clips['a.h265']
        self.ctls['b'].loader.errors['a.h265']='offline'
        self.co.seek(1);self.co.tick();self.time=1;self.co.tick()
        self.assertEqual(self.co.state,'playing')
        self.assertGreater(self.ctls['a'].idx,10)
        self.assertIn('不可用',self.co.messages['b'])

    def test_latest_seek_replaces_older_loading_target(self):
        self.co.seek(1);generation=self.co.generation;self.co.seek(6,False)
        self.assertGreater(self.co.generation,generation)
        self.ready();self.co.tick()
        self.assertEqual(self.co.position,6);self.assertEqual(self.co.state,'paused')
        self.assertEqual(self.ctls['a'].idx,61)

    def test_single_angle_mode_only_loads_selected_angle(self):
        self.co.linked=False;self.co.selected='b';self.co.seek(3)
        self.assertEqual(set(self.co.targets),{'b'})
        self.assertEqual(self.ctls['a'].mode,'live')

    def test_resume_after_loading_can_be_cancelled(self):
        self.co.seek(2);self.co.pause();self.ready();self.co.tick()
        self.assertEqual(self.co.state,'paused')

    def test_manifest_write_is_atomic_and_contains_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'match.json';save_json(p,{'tracks':{k:t.dump() for k,t in self.co.tracks.items()}})
            self.assertTrue(p.exists());self.assertFalse(p.with_suffix('.tmp').exists())

    def test_rebasing_after_calibration_keeps_absolute_playhead(self):
        self.co.position=3;old=self.co.origin+self.co.playhead()
        self.co.tracks['a'].offset=2
        delta=self.co.rebase()
        self.assertEqual(delta,2)
        self.assertEqual(self.co.origin+self.co.playhead(),old)


class BackgroundCaptureTests(unittest.TestCase):
    def setup_coordinator(self):
        self.ctls={k:Controller() for k in ['a','b']};self.time=0
        co=ReplayCoordinator(self.ctls,lambda:self.time,background=True)
        co.set_tracks({k:Track(k,'game','/record/game',[segment(),segment('b.h265',10,20,1010)]) for k in self.ctls})
        for c in self.ctls.values():
            c.loader.clips={name:dict(mkv=name,fps=10,frames=100) for name in ['a.h265','b.h265']}
        def cleanup():
            co.shutdown()
            for c in self.ctls.values():
                if c.cap:c.cap.release()
        self.addCleanup(cleanup)
        return co

    def wait(self,co,state):
        deadline=time.monotonic()+3
        while co.state!=state and time.monotonic()<deadline:
            co.tick();time.sleep(.002)
        self.assertEqual(co.state,state,co.messages)

    def test_seek_near_boundary_prebuffers_then_swaps_without_resetting_clock(self):
        co=self.setup_coordinator();co.seek(9,True)
        self.wait(co,'playing')
        self.assertTrue(all(k in co.neighbors for k in self.ctls))
        self.time=2;co.tick()
        self.assertEqual(co.state,'playing')
        self.assertTrue(all(c.seg['file']=='b.h265' for c in self.ctls.values()))
        self.assertEqual(co.playhead(),11)

    def test_superseded_background_capture_is_released(self):
        co=self.setup_coordinator();gate=threading.Event();created=[]
        def factory(info):
            c=Capture();created.append(c)
            original=c.read
            def read():gate.wait(2);return original()
            c.read=read;return c
        for c in self.ctls.values():c._open_capture=factory
        co.seek(1,False);co.tick();co.seek(6,False);gate.set()
        self.wait(co,'paused')
        active=[c.cap for c in self.ctls.values()]
        self.assertTrue(all(c.released for c in created if c not in active))
        self.assertEqual(co.position,6)


if __name__=='__main__':unittest.main()
