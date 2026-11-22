import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from match_clock import MatchClock
from sports_monitor import SportsMonitor
from sports_replay import Track


class MatchClockIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.wall=1100.;self.mono=0.
        self.app=object.__new__(SportsMonitor);self.app.root=Mock()
        self.app.game_clock=MatchClock(wall=lambda:self.wall,monotonic=lambda:self.mono)
        self.app.match_id='game';self.app.match_name='比赛';self.app.state_root=Path(self.temp.name)
        self.app.pending_new=False;self.app.active_paths={};self.app.events=[]
        self.track=Track('rk1','new','/record/new',[])
        self.app.co=SimpleNamespace(tracks={'rk1':self.track},origin=1000,duration=200,playhead=lambda:3)
        self.app.ctls={'rk1':SimpleNamespace(recording=True)}
        self.app.catalogs={'rk1':[dict(dir='/record/new',active=True)]}
        self.app.update_events=Mock();self.app.update_game_clock_ui=Mock()

    def tearDown(self):self.temp.cleanup()

    def test_start_uses_button_wall_time_not_old_replay_pointer_and_persists(self):
        self.app.start_game_clock()
        self.assertEqual(self.app.game_clock.transitions[0]['wall'],1100)
        saved=json.loads((self.app.state_root/'matches/game.json').read_text())
        self.assertEqual(saved['match_clock']['transitions'][0]['wall'],1100)
        self.assertEqual(self.app.match_time_label(103),'00:03')

    def test_start_requires_recording_this_match(self):
        self.app.ctls['rk1'].recording=False
        with patch('sports_monitor.messagebox.showinfo'):self.app.start_game_clock()
        self.assertFalse(self.app.game_clock.started)
        self.app.ctls['rk1'].recording=True;self.app.catalogs['rk1'][0]['dir']='/another/game'
        with patch('sports_monitor.messagebox.showinfo'):self.app.start_game_clock()
        self.assertFalse(self.app.game_clock.started)

    def test_start_before_first_sealed_segment_saves_explicit_recording_paths(self):
        self.app.co.tracks={};self.app.pending_new=True;self.app.active_paths={'rk1':'/record/new'}
        self.app.start_game_clock()
        saved=json.loads((self.app.state_root/'matches/game.json').read_text())
        self.assertEqual(saved['recording_paths'],{'rk1':'/record/new'})
        self.assertEqual(saved['tracks'],{});self.assertTrue(saved['match_clock']['transitions'])
        self.assertEqual(self.app.match_time_label(0),'等待录像')

    def test_match_pause_does_not_change_replay_position_and_resume_keeps_elapsed(self):
        self.app.start_game_clock();self.wall+=20;self.mono+=20
        self.app.toggle_game_clock();self.wall+=10;self.mono+=10
        self.assertEqual(self.app.game_clock.current(),20);self.assertEqual(self.app.co.playhead(),3)
        self.app.toggle_game_clock();self.wall+=5;self.mono+=5
        self.assertEqual(self.app.game_clock.current(),25)
        saved=json.loads((self.app.state_root/'matches/game.json').read_text())
        self.assertEqual(len(saved['match_clock']['transitions']),3)

    def test_restart_before_first_segment_restores_clock_paths_and_calibration(self):
        app=self.app;app.co.tracks={};app.pending_new=True;app.active_paths={'rk1':'/record/new'}
        app.calibrations={'rk1':dict(remote=1002,offset=2,uncertainty=.001,measured=1000)}
        app.start_game_clock()
        app.pending_new=False;app.game_clock=MatchClock();app.ring_overlays={}
        app.controllers=[SimpleNamespace(to_live=Mock())];app.fit_timeline=Mock();app.timeline_leave=Mock()
        app.co.set_tracks=lambda tracks:setattr(app.co,'tracks',tracks)
        segment=dict(file='a.h265',base_ms=1002000,start_ms=0,end_ms=200000,
                     start_s=0,end_s=200,frames=11840)
        track=Track('rk1','new','/record/new',[segment])
        app.use_group({'rk1':track},'恢复')
        self.assertEqual(app.match_id,'game');self.assertTrue(app.game_clock.started)
        self.assertTrue(track.calibrated);self.assertEqual(app.co.origin,1000)
        self.assertEqual(app.match_time_label(100),'00:00')

    def test_stopping_recording_freezes_its_saved_clock_while_reviewing_other_match(self):
        app=self.app;app.start_game_clock();self.wall+=20;self.mono+=20
        app.match_id='history';app.game_clock=MatchClock();app.stopping_paths={'rk1':'/record/new'}
        factory=lambda data:MatchClock(data,wall=lambda:self.wall,monotonic=lambda:self.mono)
        with patch('sports_monitor.MatchClock',side_effect=factory):app.freeze_stopped_match_clocks()
        saved=json.loads((app.state_root/'matches/game.json').read_text())
        self.assertFalse(saved['match_clock']['transitions'][-1]['running'])
        self.assertEqual(saved['match_clock']['transitions'][-1]['elapsed'],20)
        self.assertFalse(app.game_clock.started)


@unittest.skipUnless(os.environ.get('DISPLAY'),'Requires X11 DISPLAY')
class TimelineHoverTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('800x200');self.root.lower()
        self.app=object.__new__(SportsMonitor);self.app.timeline=tk.Canvas(self.root,height=112)
        self.app.timeline.pack(fill='both',expand=True);self.root.update()
        self.app.co=SimpleNamespace(origin=1000,duration=100,tracks={'rk1':object()},state='paused',seek=Mock())
        self.app.zoom=1.;self.app.view_start=0;self.app.drag=None
        data=dict(version=1,transitions=[dict(wall=1010,elapsed=0,running=True),
                  dict(wall=1020,elapsed=10,running=False),dict(wall=1040,elapsed=10,running=True)])
        self.app.game_clock=MatchClock(data)

    def tearDown(self):
        self.root.destroy();self.app=None;self.root=None
        import gc
        gc.collect()

    def event(self,position):
        return SimpleNamespace(x=52+position/100*(self.app.timeline.winfo_width()-70),y=60)

    def test_hover_and_drag_show_match_time_and_leave_removes_tooltip(self):
        app=self.app;app.timeline_hover(self.event(25))
        texts=[app.timeline.itemcget(i,'text') for i in app.timeline.find_withtag('hover') if app.timeline.type(i)=='text']
        self.assertEqual(texts,['暂停 00:10'])
        app.timeline_leave();self.assertFalse(app.timeline.find_withtag('hover'))
        app.timeline_press(self.event(35));app.timeline_release(self.event(35))
        self.assertAlmostEqual(app.co.seek.call_args[0][0],35)
        self.assertEqual(app.match_time_label(25),app.match_time_label(35))

    def test_unstarted_clock_hover_uses_recording_time(self):
        self.app.game_clock=MatchClock();self.app.timeline_hover(self.event(60))
        texts=[self.app.timeline.itemcget(i,'text') for i in self.app.timeline.find_withtag('hover') if self.app.timeline.type(i)=='text']
        self.assertEqual(texts,['录像 00:01:00'])


if __name__=='__main__':unittest.main()
