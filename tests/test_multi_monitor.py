"""Protocol and independent playback-clock regressions; no camera required."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import multi_monitor as m


class RingProtocolTests(unittest.TestCase):
    def serve(self, header, payload=b''):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        def worker():
            try:
                conn, _ = listener.accept()
                with conn:
                    self.assertEqual(conn.recv(4), b'\x0a\0\0\0')
                    conn.sendall(header+payload)
            finally:
                listener.close()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        return dict(id='test', host='127.0.0.1', replay_port=port)

    def test_recent_snapshot_uses_relative_axis_and_absolute_anchor(self):
        node = self.serve(m.HDR.pack(b'HKR1',10,123000,133000,592,4), b'hevc')
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)/'recent'
            def remux(src, dst, frames, fps):
                self.assertEqual(Path(src).read_bytes(),b'hevc')
                self.assertEqual(frames,592)
                self.assertAlmostEqual(fps,59.2)
                Path(dst).write_bytes(b'mkv')
                return True
            with patch.object(m,'remux_mkv',side_effect=remux):
                name, info = m.fetch_recent(node,10,dest,1000000)
            rec = json.loads((dest/'meta.jsonl').read_text())
            self.assertEqual(rec['start_ms'],0)
            self.assertEqual(rec['end_ms'],10000)
            self.assertEqual(rec['base_ms'],1123000)
            self.assertEqual(rec['file'],name)
            self.assertTrue(Path(info['mkv']).is_file())

    def test_truncated_transfer_is_removed(self):
        node = self.serve(m.HDR.pack(b'HKR1',10,0,10000,592,10),b'partial')
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)/'recent'
            with self.assertRaisesRegex(RuntimeError,'中断'):
                m.fetch_recent(node,10,dest,None)
            self.assertFalse(dest.exists())

    def test_invalid_header_is_removed_before_remux(self):
        node = self.serve(m.HDR.pack(b'HKR1',10,0,10000,592,2**40))
        with tempfile.TemporaryDirectory() as d, patch.object(m,'remux_mkv') as remux:
            dest=Path(d)/'recent'
            with self.assertRaises(RuntimeError):
                m.fetch_recent(node,10,dest,None)
            remux.assert_not_called()
            self.assertFalse(dest.exists())

    def test_remux_frame_mismatch_is_removed(self):
        node = self.serve(m.HDR.pack(b'HKR1',10,0,10000,592,4),b'hevc')
        with tempfile.TemporaryDirectory() as d, patch.object(m,'remux_mkv',return_value=False):
            dest=Path(d)/'recent'
            with self.assertRaisesRegex(RuntimeError,'帧数'):
                m.fetch_recent(node,10,dest,None)
            self.assertFalse(dest.exists())


class PlaybackClockTests(unittest.TestCase):
    def test_other_camera_ticks_cannot_advance_this_camera_early(self):
        ctl = object.__new__(m.UnifiedController)
        ctl.mode='replay'; ctl.cap=object(); ctl.pending=None
        ctl.paused=False; ctl.ended=False; ctl._next_tick=time.perf_counter()+1
        with patch.object(m.StudioController,'tick') as tick:
            self.assertGreater(ctl.tick(),0)
            tick.assert_not_called()

    def test_due_camera_advances(self):
        ctl = object.__new__(m.UnifiedController)
        ctl.mode='replay'; ctl.cap=object(); ctl.pending=None
        ctl.paused=False; ctl.ended=False; ctl._next_tick=0
        with patch.object(m.StudioController,'tick',return_value=.016) as tick:
            self.assertEqual(ctl.tick(),.016)
            tick.assert_called_once()

    def test_returning_live_invalidates_pending_ring_download(self):
        ctl = object.__new__(m.UnifiedController)
        ctl.ring_generation=4
        with patch.object(m.StudioController,'to_live'):
            ctl.to_live()
        self.assertEqual(ctl.ring_generation,5)

    def test_full_replay_exits_ring_snapshot(self):
        ctl = object.__new__(m.UnifiedController)
        ctl.ring_generation=0
        ctl.args=SimpleNamespace(out_dir='/state/rk1/state')
        ctl.opened={'dir':'/state/rk1/ring-cache/recent_123'}
        with patch.object(ctl,'close_session') as close, patch.object(m.StudioController,'to_replay') as replay:
            ctl.to_replay()
            close.assert_called_once_with(quiet=True)
            replay.assert_called_once()
        self.assertEqual(ctl.ring_generation,1)

    def test_full_replay_keeps_explicitly_opened_archive(self):
        ctl = object.__new__(m.UnifiedController)
        ctl.ring_generation=0
        ctl.args=SimpleNamespace(out_dir='/state/rk1/state')
        ctl.opened={'dir':'/recordings/stage1_123'}
        with patch.object(ctl,'close_session') as close, patch.object(m.StudioController,'to_replay'):
            ctl.to_replay()
            close.assert_not_called()


if __name__=='__main__':
    unittest.main()
