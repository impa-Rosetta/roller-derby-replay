import unittest
from unittest.mock import Mock, MagicMock
from types import SimpleNamespace
from sports_replay import ReplayCoordinator, Track


WALL=1795337617.35


def segment(start=0,end=10,wall=WALL):
    return dict(file='part.h265',start_s=start,end_s=end,start_ms=start*1000,end_ms=end*1000,
                base_ms=wall*1000,frames=592,bytes=37000000)


class TimelineOriginTests(unittest.TestCase):
    def setUp(self):
        self.clock=0
        self.co=ReplayCoordinator({'rk1':Mock(),'rk2':Mock()},now=lambda:self.clock)

    def test_empty_new_directories_have_zero_duration_until_first_segment_arrives(self):
        track=Track('rk1','new','/record/new',[])
        self.co.set_tracks({'rk1':track})
        self.assertEqual(self.co.duration,0)
        track.segments=[segment()]
        self.assertAlmostEqual(self.co.duration,10)
        self.assertAlmostEqual(self.co.origin,WALL)
        self.assertEqual(self.co.playhead(),0)

    def test_rebase_before_first_segment_cannot_promote_epoch_to_match_duration(self):
        track=Track('rk1','new','/record/new',[],calibrated=True,
                    clock_samples=[dict(remote=WALL,offset=.05)])
        self.co.set_tracks({'rk1':track});self.co.rebase()
        self.clock=15;track.segments=[segment()]
        self.co.rebase()
        self.assertAlmostEqual(self.co.duration,10)
        self.assertEqual(self.co.playhead(),0)

    def test_empty_other_camera_does_not_override_valid_origin(self):
        tracks={'rk1':Track('rk1','a','/a',[segment()]),'rk2':Track('rk2','b','/b',[])}
        self.co.set_tracks(tracks)
        self.assertAlmostEqual(self.co.duration,10)
        self.assertAlmostEqual(self.co.origin,WALL)

    def test_growing_segments_keep_stable_origin_and_relative_position(self):
        track=Track('rk1','a','/a',[]);self.co.set_tracks({'rk1':track})
        track.segments=[segment()];self.assertAlmostEqual(self.co.duration,10)
        self.co.position=5;self.co.state='paused'
        track.segments.append(segment(10,20,WALL+10))
        self.assertAlmostEqual(self.co.duration,20);self.assertEqual(self.co.playhead(),5)
        self.assertAlmostEqual(self.co.origin,WALL)

    def test_next_empty_match_does_not_reuse_previous_match_origin(self):
        self.co.set_tracks({'rk1':Track('rk1','old','/old',[segment()])})
        new=Track('rk1','new','/new',[]);self.co.set_tracks({'rk1':new})
        self.assertEqual(self.co.duration,0)
        new.segments=[segment(wall=WALL+86400)]
        self.assertAlmostEqual(self.co.duration,10)
        self.assertAlmostEqual(self.co.origin,WALL+86400)

    def test_catalog_keeps_initialization_pending_until_actual_segments_arrive(self):
        from sports_monitor import SportsMonitor
        app=object.__new__(SportsMonitor)
        app.catalogs={};app.batch_busy=True;app.ctls={'rk1':Mock(),'rk2':Mock()}
        app.pending_new=True;app.active_paths={'rk1':'/rk1/new','rk2':'/rk2/new'}
        app.calibrations={};app.co=self.co;app.match_combo=MagicMock();app.save_match=Mock()
        app.ring_overlays={};app.after_stop=False
        def catalog(ready):
            return {k:[dict(name='new',dir=d,segments=[segment()] if k in ready else [])]
                    for k,d in app.active_paths.items()}
        app.apply_catalog(catalog(set()))
        self.assertTrue(app.pending_new);self.assertFalse(app.co.tracks)
        app.apply_catalog(catalog({'rk1'}))
        self.assertTrue(app.pending_new);self.assertAlmostEqual(app.co.duration,10)
        app.apply_catalog(catalog({'rk1','rk2'}))
        self.assertFalse(app.pending_new);self.assertEqual(len(app.co.tracks),2)
        self.assertAlmostEqual(app.co.duration,10)

    def test_initial_catalog_prefers_active_match_over_longer_history(self):
        from sports_monitor import SportsMonitor
        app=object.__new__(SportsMonitor);app.catalogs={};app.batch_busy=False
        app.ctls={k:SimpleNamespace(remote_busy=False,recording=False,feed=Mock()) for k in ['rk1','rk2']}
        app.controllers=list(app.ctls.values())
        app.pending_new=False;app.co=SimpleNamespace(tracks={});app.match_combo=MagicMock();app.use_group=Mock()
        catalog={k:[dict(name='old',dir='/'+k+'/old',segments=[segment(end=100)],active=False),
                    dict(name='current',dir='/'+k+'/current',segments=[segment(wall=WALL+86400)],active=True)]
                 for k in app.ctls}
        app.apply_catalog(catalog)
        tracks=app.use_group.call_args[0][0]
        self.assertTrue(all(t.name=='current' for t in tracks.values()))


if __name__=='__main__':unittest.main()
