import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sports_monitor import SportsMonitor
from sports_replay import Track
from storage_manager import StorageManager
from test_storage_service import LocalTransport, NAME


@unittest.skipUnless(os.environ.get('DISPLAY'),'Requires X11 DISPLAY')
class StorageUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.root=tk.Tk();self.root.title('Storage management UI verification');self.root.lower()
        self.app=SimpleNamespace(root=self.root,nodes=[dict(id='rk1',remote='test',remote_root=str(self.base/'rk'))],
                ctls={'rk1':SimpleNamespace(archive_dir=str(self.base/'dgx'))},state_root=self.base/'state',
                controllers=[],batch_busy=False,storage_before_delete=Mock(),storage_after_operation=Mock(),
                co=SimpleNamespace(origin=0,seek=Mock()),use_group=Mock(),match_name='test',match_combo=Mock())
        with patch.object(StorageManager,'refresh'):
            self.ui=StorageManager(self.app)
        self.ui.service=LocalTransport(self.app.nodes,{'rk1':str(self.base/'dgx')},self.base/'state')
        p=self.base/'rk'/NAME;p.mkdir(parents=True)
        (p/'seg_000.h265').write_bytes(b'fixture')
        (p/'meta.jsonl').write_text(json.dumps(dict(file='seg_000.h265',start_ms=0,end_ms=10000,
                                                  frames=592,base_ms=1795240000000))+'\n')
        self.ui.show_snapshot(self.ui.service.snapshot());self.ui.last_scan=10**12
        self.root.update()

    def tearDown(self):
        self.ui.busy=False;self.ui.close();self.root.destroy();self.temp.cleanup()

    def test_table_filter_multiselection_and_action_geometry(self):
        self.assertEqual(len(self.ui.tree.get_children()),1)
        self.ui.tree.selection_set(*self.ui.tree.get_children());self.root.update()
        self.assertIn('1 份录像',self.ui.selection_label.cget('text'))
        for b in self.ui.action_buttons:
            self.assertGreater(b.winfo_height(),20)
            self.assertLessEqual(b.winfo_rootx()+b.winfo_width(),self.ui.window.winfo_rootx()+self.ui.window.winfo_width())
        self.ui.search.set('no-match');self.assertEqual(len(self.ui.tree.get_children()),0)
        self.ui.search.set('');self.ui.location.current(2);self.ui.render_rows()
        self.assertEqual(len(self.ui.tree.get_children()),0)
        self.ui.location.current(0);self.ui.render_rows();self.ui.only_archived.set(True);self.ui.render_rows()
        self.assertEqual(len(self.ui.tree.get_children()),0)

    def test_cancel_delete_does_not_start_operation(self):
        self.ui.tree.selection_set(*self.ui.tree.get_children())
        with patch('storage_manager.messagebox.askyesno',return_value=False):self.ui.delete('rk')
        self.assertFalse(self.ui.busy);self.app.storage_before_delete.assert_not_called()
        self.assertTrue((self.base/'rk'/NAME).exists())

    def test_delete_updates_list_and_reports_completion(self):
        self.ui.tree.selection_set(*self.ui.tree.get_children())
        with patch('storage_manager.messagebox.askyesno',return_value=True):self.ui.delete('rk')
        import time
        end=time.monotonic()+5
        while self.ui.busy and time.monotonic()<end:
            self.ui.pump();self.root.update();time.sleep(.02)
        self.assertFalse(self.ui.busy);self.assertEqual(len(self.ui.tree.get_children()),0)
        self.assertIn('成功 1',self.ui.status.cget('text'))
        self.app.storage_before_delete.assert_called_once();self.app.storage_after_operation.assert_called_once()


class StorageReferenceTests(unittest.TestCase):
    def test_delete_rebinds_current_and_saved_match_to_dgx_preserving_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'matches').mkdir()
            remote='/home/admin/hk_record/'+NAME;local='/home/ifit/archive/rk1/'+NAME
            segment=dict(file='seg_000.h265',start_s=0,end_s=10,start_ms=0,end_ms=10000,
                         frames=592,bytes=7,base_ms=1000000,dir=remote)
            track=Track('rk1',NAME,remote,[segment],offset=1.5,calibrated=True)
            record=dict(id='test',name='test',events=[dict(position=3)],tracks={'rk1':track.dump()})
            (root/'matches/test.json').write_text(json.dumps(record))
            row=dict(node='rk1',directory=remote,name=NAME,segments=[segment])
            other=dict(row,directory=local,location='dgx')
            app=object.__new__(SportsMonitor);app.state_root=root;app.ring_overlays={}
            app.co=SimpleNamespace(tracks={'rk1':track},set_tracks=Mock())
            import threading
            app.ctls={'rk1':SimpleNamespace(loader=SimpleNamespace(cv=threading.Condition(),clips={},bad=set(),
                                  errors={},fail_count={},cache_dir=str(root)))}
            app.catalogs={'rk1':[dict(dir=remote)]};app.save_match=Mock();app.refresh_catalog=Mock()
            app.events=[dict(position=3)];app.update_events=Mock()
            app.storage_after_operation('delete',[(row,True,{})],dict(rows=[other]))
            saved=json.loads((root/'matches/test.json').read_text())
            self.assertEqual(saved['tracks']['rk1']['directory'],local)
            self.assertEqual(saved['tracks']['rk1']['offset'],1.5)
            self.assertTrue(saved['tracks']['rk1']['calibrated'])
            self.assertEqual(saved['events'],[dict(position=3)])
            tracks=app.co.set_tracks.call_args[0][0]
            self.assertEqual(tracks['rk1'].segments[0]['local_dir'],local)


if __name__=='__main__':unittest.main()
