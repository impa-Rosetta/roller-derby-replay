import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import storage_agent as agent
from storage_service import StorageService
from sports_replay import camera_catalog


NAME = 'stage1_20261121_180000'


class LocalTransport(StorageService):
    """Execute the exact node operations on an isolated fixture filesystem."""
    def call(self,node,location,action,**kwargs):
        root = self.nodes[node]['remote_root'] if location == 'rk' else self.archive_roots[node]
        return agent.execute(dict(root=root,action=action,remote_recordings=location=='rk',**kwargs))


class StorageTests(unittest.TestCase):
    def setUp(self):
        # Earlier GUI tests may leave Tk reference cycles. Finalize them on
        # the GUI/main thread before this fixture starts scan worker threads.
        import gc
        gc.collect()
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.remote = self.root/'rk';self.local = self.root/'dgx'
        self.session = self.remote/NAME;self.session.mkdir(parents=True)
        (self.session/'seg_000.h265').write_bytes(b'video fixture\0'*31)
        (self.session/'meta.jsonl').write_text(json.dumps(dict(file='seg_000.h265',start_ms=0,
                         end_ms=10000,base_ms=1795240000000,frames=592,bytes=434))+'\n')
        self.service = LocalTransport([dict(id='rk1',remote='test',remote_root=str(self.remote))],
                                     {'rk1':str(self.local)},self.root/'state')
        self.running = patch('storage_agent.recording_running',return_value=False);self.running.start()

    def tearDown(self):
        self.running.stop();self.temp.cleanup()

    def row(self):
        return next(r for r in self.service.snapshot()['rows'] if r['location']=='rk')

    def copy(self,command,**kwargs):
        destination = Path(command[-1]);destination.mkdir(exist_ok=True)
        shutil.copytree(self.session,destination,dirs_exist_ok=True)
        return subprocess.CompletedProcess(command,0,'','')

    def test_snapshot_reports_bytes_duration_and_actual_disk_space(self):
        row = self.row()
        self.assertEqual(row['frames'],592);self.assertEqual(row['seconds'],10)
        self.assertEqual(row['bytes'],sum(p.stat().st_size for p in self.session.iterdir()))
        self.assertFalse(row['other_copy']);self.assertGreater(self.service.snapshot()['disks']['rk1']['available'],0)

    def test_deletion_rejects_traversal_and_changed_selection(self):
        for name in ['../stage1_20261121_180000','', '/', NAME+'/child']:
            with self.assertRaises(ValueError):agent.checked_directory(self.remote,name)
        row = self.row();(self.session/'meta.jsonl').write_text('changed')
        with self.assertRaisesRegex(ValueError,'变化'):self.service.delete(row)
        self.assertTrue(self.session.exists())

    def test_active_recorder_blocks_delete_and_archive(self):
        row = self.row()
        with patch('storage_agent.recording_running',return_value=True):
            with self.assertRaisesRegex(ValueError,'正在录制'):self.service.delete(row)
            with self.assertRaisesRegex(ValueError,'正在录制'):self.service.archive(row)
        self.assertTrue(self.session.exists())

    def test_delete_removes_only_selected_copy_and_writes_audit(self):
        row = self.row();self.local.mkdir();shutil.copytree(self.session,self.local/NAME)
        self.service.delete(row)
        self.assertFalse(self.session.exists());self.assertTrue((self.local/NAME/'seg_000.h265').exists())
        self.assertEqual(json.loads((self.root/'state/storage-operations.jsonl').read_text())['status'],'OK')

    def test_verified_archive_keeps_source_then_local_replay_survives_source_delete(self):
        row = self.row()
        with patch('storage_service.subprocess.run',side_effect=self.copy):self.service.archive(row)
        self.assertTrue(self.session.exists())
        rows = self.service.snapshot()['rows'];self.assertTrue(all(r['archive_verified'] for r in rows))
        self.service.delete(row)
        with patch('sports_replay.remote_python',side_effect=RuntimeError('offline')):
            catalog = camera_catalog(dict(id='rk1',remote='unreachable'),str(self.local))
        self.assertEqual(len(catalog),1)
        self.assertEqual(catalog[0]['segments'][0]['local_dir'],str(self.local/NAME))

    def test_same_size_corruption_fails_hash_check_and_never_deletes_source(self):
        def corrupt(command,**kwargs):
            result = self.copy(command,**kwargs)
            p = Path(command[-1])/'seg_000.h265';p.write_bytes(b'x'*p.stat().st_size)
            return result
        with patch('storage_service.subprocess.run',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'校验失败'):self.service.archive(self.row())
        self.assertTrue(self.session.exists());self.assertFalse((self.local/NAME).exists())
        self.assertTrue((self.local/('.'+NAME+'.partial')).exists())

    def test_failed_transfer_retains_partial_and_retry_verifies_it(self):
        def fail(command,**kwargs):
            self.copy(command);return subprocess.CompletedProcess(command,23,'','connection lost')
        with patch('storage_service.subprocess.run',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'传输失败'):self.service.archive(self.row())
        with patch('storage_service.subprocess.run',side_effect=self.copy):self.service.archive(self.row())
        self.assertTrue((self.local/NAME/agent.PROOF).exists())

    def test_existing_archive_is_verified_instead_of_trusted_by_name(self):
        self.local.mkdir();shutil.copytree(self.session,self.local/NAME)
        p = self.local/NAME/'seg_000.h265';p.write_bytes(b'x'*p.stat().st_size)
        with self.assertRaisesRegex(ValueError,'校验失败'):self.service.archive(self.row())
        self.assertTrue(self.session.exists());self.assertFalse((self.local/NAME/agent.PROOF).exists())

    def test_symlink_cannot_escape_recording_root(self):
        outside = self.root/'outside';outside.mkdir()
        other = self.remote/'stage1_20261121_190000'
        try:other.symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlink privilege required')
        with self.assertRaises(ValueError):agent.checked_directory(self.remote,other.name)
        (self.session/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'符号链接'):agent.inventory(self.session)


if __name__=='__main__':unittest.main()
