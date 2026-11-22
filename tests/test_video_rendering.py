import os
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import threading

import numpy as np
from sports_monitor import CameraSurface, SportsMonitor
from sports_replay import Track, ReplayCoordinator


class DecoderRetirementTests(unittest.TestCase):
    def test_slow_decoder_release_does_not_block_installing_next_segment(self):
        gate=threading.Event();started=threading.Event();finished=threading.Event()
        old=Mock()
        def release():
            started.set();gate.wait(2);finished.set()
        old.release.side_effect=release
        ctl=SimpleNamespace(cap=old,_next_seg=lambda s:None)
        co=ReplayCoordinator({'rk1':ctl},background=True)
        new=Mock()
        try:
            co.install_capture('rk1',dict(cap=new,drawn=object(),idx=1,fps=59.2,
                                         seg=dict(start_s=0),info=dict(frames=600)))
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.is_set());self.assertIs(ctl.cap,new)
        finally:
            gate.set();finished.wait(1);co.shutdown()


@unittest.skipUnless(os.environ.get('DISPLAY'),'Requires X11 DISPLAY')
class VideoRenderingTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('640x480');self.root.lower()
        self.frame=np.zeros((204,244,3),dtype=np.uint8);self.frame[:,:,2]=255
        self.ctl=SimpleNamespace(current_frame=lambda:self.frame,mode='replay',recording=False,
                                  feed=SimpleNamespace(alive=lambda:False,image_t=0),live_fps=lambda:59.2)
        co=SimpleNamespace(messages={},state='playing',linked=True,tracks={'rk1':object()},
                           participants=['rk1'],playhead=lambda:1.)
        app=SimpleNamespace(ctls={'rk1':self.ctl},co=co,selected='rk1',select=lambda k:None,focus_camera=lambda k:None)
        self.surface=CameraSurface(self.root,app,'rk1');self.surface.canvas.pack(fill='both',expand=True)
        self.root.update()

    def tearDown(self):
        self.root.destroy();self.surface=None;self.root=None
        import gc
        gc.collect()

    def test_updates_reuse_image_and_count_only_new_source_frames(self):
        s=self.surface;s.draw();self.root.update_idletasks()
        item=s.image_item;photo=str(s.photo);items=s.canvas.find_all()
        self.assertEqual(s.presented_count,1)
        s.draw();self.assertEqual(s.presented_count,1)
        self.frame=self.frame.copy();self.frame[:,:,2]=0;self.frame[:,:,1]=255
        s.draw();self.root.update_idletasks()
        self.assertEqual(s.presented_count,2);self.assertEqual(s.image_item,item)
        self.assertEqual(str(s.photo),photo);self.assertEqual(s.canvas.find_all(),items)
        pixel=s.canvas.tk.call(str(s.photo),'get',s.photo_size[0]//2,s.photo_size[1]//2)
        self.assertEqual(tuple(pixel),(0,255,0))

    def test_resize_preserves_full_aspect_and_idle_does_not_report_live_fps(self):
        s=self.surface;s.draw()
        self.root.geometry('900x650');self.root.update();s.draw()
        w,h=s.photo_size
        self.assertLess(abs(w/h-244/204),.01)
        self.assertLessEqual(w,s.canvas.winfo_width());self.assertLessEqual(h,s.canvas.winfo_height())
        with patch('sports_monitor.time.monotonic',return_value=s.presented[-1]+3):
            self.assertEqual(s.display_fps(),0)

    def test_timeline_motion_reuses_static_tracks(self):
        app=object.__new__(SportsMonitor);app.timeline=tk.Canvas(self.root,width=600,height=112)
        app.timeline.pack();self.root.update()
        segment=dict(file='a.h265',base_ms=1000000,start_s=0,end_s=10,start_ms=0,end_ms=10000,frames=592)
        app.co=SimpleNamespace(tracks={'rk1':Track('rk1','test','/test',[segment])},origin=1000,
                               duration=10,state='playing',playhead=lambda:1)
        app.nodes=[dict(id='rk1')];app.selected='rk1';app.events=[];app.drag=None
        app.zoom=1.;app.view_start=0.;app.position_label=tk.Label(self.root)
        app.draw_timeline()
        static=set(app.timeline.find_all())-set(app.timeline.find_withtag('playhead'))
        app.co.playhead=lambda:2;app.draw_timeline()
        self.assertEqual(static,set(app.timeline.find_all())-set(app.timeline.find_withtag('playhead')))


if __name__=='__main__':unittest.main()
