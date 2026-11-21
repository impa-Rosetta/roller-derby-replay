import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import cv2
from replay_capture_rk import HardwareReplayCapture


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


if __name__=='__main__':unittest.main()
