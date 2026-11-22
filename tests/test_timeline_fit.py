import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from sports_monitor import SportsMonitor


class TimelineFitTests(unittest.TestCase):
    def setUp(self):
        self.app=object.__new__(SportsMonitor)
        self.app.co=SimpleNamespace(duration=3600,playhead=lambda:3500)
        self.app.zoom=1.;self.app.view_start=0;self.app.timeline_auto_fit=True

    def test_full_recording_remains_visible_as_duration_grows_or_changes(self):
        for duration in [0,10,3600,7200,10800,20]:
            self.app.co.duration=duration;self.app.view_start=500
            self.assertEqual(self.app.timeline_range(),(0,max(1,duration)))
            self.assertEqual(self.app.view_start,0)

    def test_explicit_zoom_can_be_restored_to_persistent_auto_fit(self):
        app=self.app;app.change_zoom(1.5)
        self.assertFalse(app.timeline_auto_fit);self.assertLess(app.timeline_range()[1],3600)
        app.fit_timeline();app.co.duration=8000
        self.assertEqual(app.timeline_range(),(0,8000));self.assertTrue(app.timeline_auto_fit)
        app.change_zoom(1/1.5);self.assertTrue(app.timeline_auto_fit)

    def test_normal_mouse_wheel_and_shift_wheel_do_not_change_full_view(self):
        app=self.app
        for event in [SimpleNamespace(delta=120,state=0),SimpleNamespace(num=4,state=0),
                      SimpleNamespace(num=5,state=1)]:
            self.assertEqual(app.timeline_wheel(event),'break')
            self.assertEqual(app.timeline_range(),(0,3600));self.assertTrue(app.timeline_auto_fit)
        app.pan(.15);self.assertEqual(app.timeline_range(),(0,3600))

    def test_control_wheel_is_deliberate_zoom_and_zooming_out_restores_full_view(self):
        app=self.app;app.timeline_wheel(SimpleNamespace(num=4,state=4))
        self.assertFalse(app.timeline_auto_fit)
        app.timeline_wheel(SimpleNamespace(num=5,state=4))
        self.assertTrue(app.timeline_auto_fit);self.assertEqual(app.timeline_range(),(0,3600))

    def test_entire_match_is_selectable_at_any_window_width(self):
        app=self.app;app.timeline=Mock()
        for width in [500,1200,2500]:
            app.timeline.winfo_width.return_value=width
            for duration in [3600,7200]:
                app.co.duration=duration
                self.assertEqual(app.timeline_time(52),0)
                self.assertEqual(app.timeline_time(width-18),duration)


if __name__=='__main__':unittest.main()
