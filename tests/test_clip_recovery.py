from pathlib import Path
import tempfile
import threading
import time
import unittest
from studio_services_rk import ClipLoader


class CacheRecoveryTests(unittest.TestCase):
    def test_evicted_mkv_invalidates_in_memory_cache_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            loader=ClipLoader(None,d,cache_max_gb=0)
            path=Path(d)/'a.mkv';path.write_bytes(b'complete')
            loader.clips['a.h265']=dict(mkv=str(path))
            self.assertIsNotNone(loader.clip('a.h265'))
            path.unlink()
            self.assertIsNone(loader.clip('a.h265'))
            loader.request(dict(file='a.h265'))
            self.assertEqual(loader.want['file'],'a.h265')

    def test_new_seek_drops_old_queue_and_does_not_retry_obsolete_transfer(self):
        with tempfile.TemporaryDirectory() as d:
            loader=ClipLoader(None,d,cache_max_gb=0)
            started=threading.Event();release=threading.Event();attempts=[]
            path=Path(d)/'new.mkv';path.write_bytes(b'complete')
            def ensure(seg):
                attempts.append(seg['file'])
                if seg['file']=='old':started.set();release.wait(2);raise OSError('old transfer failed')
                return dict(mkv=str(path))
            loader._ensure=ensure
            loader.request(dict(file='old'));loader.start()
            try:
                self.assertTrue(started.wait(2))
                loader.queue=[dict(file='stale-prefetch')]
                loader.clear_pending();loader.request(dict(file='new'));release.set()
                deadline=time.monotonic()+2
                while loader.clip('new') is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertIsNotNone(loader.clip('new'))
                self.assertEqual(attempts,['old','new'])
            finally:release.set();loader.stop();loader.join(2)


if __name__=='__main__':unittest.main()
