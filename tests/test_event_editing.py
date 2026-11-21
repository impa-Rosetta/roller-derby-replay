import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sports_monitor import SportsMonitor


class EventEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=object.__new__(SportsMonitor)
        self.app.root=Mock();self.app.match_id='game';self.app.match_name='比赛'
        self.app.state_root=Path(self.temp.name);self.app.co=SimpleNamespace(tracks={})
        self.app.events=[dict(position=123.456,note='旧备注'),dict(position=200,note='另一标记')]
        self.app.event_list=Mock();self.app.event_list.curselection.return_value=(0,)
        self.app.update_events=Mock()

    def tearDown(self):self.temp.cleanup()

    def test_edit_chinese_note_persists_without_changing_timestamp_or_other_events(self):
        with patch('sports_monitor.simpledialog.askstring',return_value='  关键判罚：需要复核  '):
            self.assertEqual(self.app.edit_event(),'break')
        saved=json.loads((self.app.state_root/'matches/game.json').read_text())
        self.assertEqual(saved['events'][0],dict(position=123.456,note='关键判罚：需要复核'))
        self.assertEqual(saved['events'][1],dict(position=200,note='另一标记'))
        self.app.event_list.selection_set.assert_called_once_with(0)

    def test_cancel_preserves_original_and_does_not_write(self):
        with patch('sports_monitor.simpledialog.askstring',return_value=None):self.app.edit_event()
        self.assertEqual(self.app.events[0]['note'],'旧备注')
        self.assertFalse((self.app.state_root/'matches').exists())

    def test_blank_note_preserves_original(self):
        with patch('sports_monitor.simpledialog.askstring',return_value='  '),patch('sports_monitor.messagebox.showinfo'):
            self.app.edit_event()
        self.assertEqual(self.app.events[0]['note'],'旧备注')
        self.assertFalse((self.app.state_root/'matches').exists())

    def test_selection_required(self):
        self.app.event_list.curselection.return_value=()
        with patch('sports_monitor.simpledialog.askstring') as dialog:self.app.edit_event()
        dialog.assert_not_called()

    def test_switching_match_during_dialog_cannot_change_old_event(self):
        def change(*args,**kwargs):
            self.app.match_id='another-game';return '新备注'
        with patch('sports_monitor.simpledialog.askstring',side_effect=change):self.app.edit_event()
        self.assertEqual(self.app.events[0]['note'],'旧备注')
        self.assertFalse((self.app.state_root/'matches').exists())


if __name__=='__main__':unittest.main()
