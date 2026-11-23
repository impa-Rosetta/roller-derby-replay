import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import cv2
import numpy as np
from replay_capture_rk import HardwareReplayCapture,CachedReplayCapture


class ExactSeekTests(unittest.TestCase):
    def capture(self):
        cap=object.__new__(HardwareReplayCapture)
        cap.Gst=SimpleNamespace(Format=SimpleNamespace(TIME=3),
                                SeekFlags=SimpleNamespace(FLUSH=1,ACCURATE=2),
                                MSECOND=1000000)
        cap.frames=1800;cap.fps=59.25;cap.position=0
        cap.pipe=Mock();cap.pipe.seek_simple.return_value=True
        cap.report=Mock()
        return cap

    def test_seek_matches_remux_pts_rounding_instead_of_previous_frame_interval(self):
        cap=self.capture()
        self.assertTrue(cap.set(cv2.CAP_PROP_POS_FRAMES,110))
        cap.pipe.seek_simple.assert_called_once_with(3,3,1857000000)
        self.assertEqual(cap.position,110)

    def test_failed_seek_cannot_return_old_frame_as_new_position(self):
        cap=self.capture();cap.pipe.seek_simple.return_value=False
        self.assertFalse(cap.set(cv2.CAP_PROP_POS_FRAMES,110))
        self.assertEqual(cap.position,0)
        self.assertEqual(cap.read(),(False,None))
        cap.report.assert_called_once()

    def test_frame_count_uses_actual_encoded_count(self):
        cap=self.capture()
        self.assertEqual(cap.get(cv2.CAP_PROP_FRAME_COUNT),1800)
        self.assertEqual(cap.get(cv2.CAP_PROP_FPS),59.25)


class FrameCacheTests(unittest.TestCase):
    def capture(self,budget=10000):
        source=Mock();cursor=[0]
        def seek(prop,index):cursor[0]=index;return True
        def read():
            index=cursor[0];cursor[0]+=1
            return True,np.full((2,2,3),index,dtype=np.uint8)
        source.set.side_effect=seek;source.read.side_effect=read
        cap=CachedReplayCapture(source,dict(frames=200,fps=60),max_bytes=budget,backfill=4)
        return cap,source

    def test_forward_backward_and_resume_preserve_exact_frames_without_reseek(self):
        cap,source=self.capture()
        for index in range(10):self.assertEqual(int(cap.read()[1][0,0,0]),index)
        for index in [8,7,8,9,10,11]:
            self.assertTrue(cap.set(cv2.CAP_PROP_POS_FRAMES,index))
            self.assertEqual(int(cap.read()[1][0,0,0]),index)
        source.set.assert_not_called();self.assertEqual(source.read.call_count,12)

    def test_uncached_reverse_step_backfills_window_once(self):
        cap,source=self.capture();cap.read_at(100);cap.read_at(99)
        self.assertEqual(source.set.call_count,2)
        self.assertEqual(source.set.call_args.args[1],95)
        for index in [98,97,96,95,96,100,101]:
            self.assertEqual(int(cap.read_at(index)[1][0,0,0]),index)
        self.assertEqual(source.set.call_count,2)

    def test_cache_memory_is_bounded_and_released(self):
        cap,source=self.capture(budget=36)
        for index in range(20):cap.read()
        self.assertLessEqual(cap.cache_bytes,36);self.assertLessEqual(len(cap.cache),3)
        cap.release();cap.release();source.release.assert_called_once()
        self.assertEqual(cap.cache_bytes,0);self.assertEqual(cap.read(),(False,None))

    def test_failed_seek_never_returns_previous_frame(self):
        cap,source=self.capture();cap.read_at(100);source.set.return_value=False;source.set.side_effect=None
        self.assertEqual(cap.read_at(50),(False,None))

    def test_parked_segment_reuses_cached_picture_and_lazily_reopens_for_miss(self):
        cap,source=self.capture();other,new_source=self.capture()
        cap.factory=Mock(return_value=new_source)
        for _ in range(10):cap.read()
        cap.park();self.assertIsNone(cap.source);source.release.assert_called_once()
        self.assertEqual(int(cap.read_at(8)[1][0,0,0]),8);cap.factory.assert_not_called()
        self.assertEqual(int(cap.read_at(100)[1][0,0,0]),100);cap.factory.assert_called_once()
        cap.release();new_source.release.assert_called_once()


if __name__=='__main__':unittest.main()
