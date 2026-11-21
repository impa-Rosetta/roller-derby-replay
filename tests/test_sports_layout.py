"""Exercise real Tk geometry: place() keeps unspecified relative dimensions."""
import os
import tkinter as tk
import unittest
from types import SimpleNamespace
from sports_monitor import SportsMonitor


@unittest.skipUnless(os.environ.get('DISPLAY'),'Requires X11 DISPLAY (or Xvfb)')
class LayoutRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('1400x950+0+0')
        self.root.title('Layout regression check')
        self.root.lower()
        self.app=object.__new__(SportsMonitor)
        self.app.root=self.root;self.app.focused=None
        self.app.ctls={key:object() for key in ['rk1','rk2','rk3','rk4']}
        self.app.co=SimpleNamespace(selected='rk1')
        self.app.stage=tk.Frame(self.root);self.app.stage.pack(fill='both',expand=True)
        self.app.matrix=tk.Frame(self.app.stage);self.app.side=tk.Frame(self.app.stage)
        self.app.surfaces={key:SimpleNamespace(canvas=tk.Canvas(self.app.matrix)) for key in self.app.ctls}
        self.root.update();self.app.layout();self.root.update()

    def tearDown(self):self.root.destroy()

    def assert_grid(self):
        mw,mh=self.app.matrix.winfo_width(),self.app.matrix.winfo_height()
        for key,s in self.app.surfaces.items():
            c=s.canvas
            self.assertTrue(c.winfo_ismapped(),key)
            self.assertLessEqual(c.winfo_x()+c.winfo_width(),mw,key)
            self.assertLessEqual(c.winfo_y()+c.winfo_height(),mh,key)
            self.assertLess(abs(c.winfo_width()-(mw/2-1)),2,key)
            self.assertLess(abs(c.winfo_height()-(mh/2-1)),2,key)

    def test_every_camera_can_enlarge_and_return_repeatedly_without_clipping(self):
        for _ in range(3):
            for key in self.app.ctls:
                self.app.focus_camera(key);self.root.update()
                canvas=self.app.surfaces[key].canvas
                self.assertEqual(canvas.winfo_x(),0,key)
                self.assertEqual(canvas.winfo_y(),0,key)
                self.assertEqual(canvas.winfo_width(),self.app.matrix.winfo_width(),key)
                self.assertEqual(canvas.winfo_height(),self.app.matrix.winfo_height(),key)
                for other,s in self.app.surfaces.items():
                    self.assertEqual(bool(s.canvas.winfo_ismapped()),other==key)
                self.app.grid_view();self.root.update();self.assert_grid()

    def test_resizing_and_switching_directly_between_focused_cameras(self):
        for size in ['1200x800','1800x1050','1400x950']:
            self.root.geometry(size);self.root.update()
            for key in self.app.ctls:
                self.app.focus_camera(key);self.root.update()
                c=self.app.surfaces[key].canvas
                self.assertEqual(c.winfo_width(),self.app.matrix.winfo_width())
                self.assertEqual(c.winfo_height(),self.app.matrix.winfo_height())
            self.app.grid_view();self.root.update();self.assert_grid()


if __name__=='__main__':unittest.main()
