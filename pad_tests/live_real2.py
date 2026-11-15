import sys, os, time, urllib.request, types, json, threading
sys.path.insert(0, '/home/ifit/xjh')
import studio_app as sa, referee_data as rd, numpy as np, cv2

args = types.SimpleNamespace(
    host='127.0.0.1', live_port=8601, thumb_port=0, display_width=800,
    out_dir='/home/ifit/xjh/replay/archive', cache_dir='/home/ifit/xjh/replay/cache',
    cache_max_gb=50, remote=None, remote_root='/home/syd/var_replay',
    archive_interval=3.0, rec_segment=10.0, rec_ring=90.0, rec_min_free_gb=1.0,
    speed=0, referee_port=0)

ctl = sa.StudioController(args)
srv = rd.PadServer(ctl, port=int(os.environ.get('PP','18820')))
srv.start()
stop = False
ticks = [0]
def loop():
    while not stop:
        ctl.tick(); ticks[0] += 1
        # 没有相机时 feed.image 为 None；塞测试图，验证编码链路
        ctl.feed.image = cv2.morphologyEx(np.random.randint(0,255,(1280,720,3), np.uint8), cv2.MORPH_CLOSE, np.ones((41,41),np.uint8))
        srv.pump(); time.sleep(0.03)
threading.Thread(target=loop, daemon=True).start()
time.sleep(3)

B='http://127.0.0.1:%d' % srv.port
s = json.loads(urllib.request.urlopen(B+'/api/status', timeout=3).read())
print('feed.image_n', ctl.feed.image_n, 'padseq', srv.hub.pad.seq, 'liveseq', srv.hub.live.seq, 'cli', srv.hub.pad.clients, 'gen', srv.hub.pad.gen)
print('status:', {k: s[k] for k in ('mode','info','page','pg','btn0','hl')}, 'ticks', ticks[0])
r = urllib.request.urlopen(B+'/stream.mjpg', timeout=5)
d=b''; t0=time.time()
try:
    while time.time()-t0 < 3: d += r.read(65536)
except Exception as e: print('read stop', type(e).__name__)
r.close()
print('live /stream: frames', d.count(b'\xff\xd8'), 'bytes', len(d))
r = urllib.request.urlopen(B+'/snapshot.jpg', timeout=5); j=r.read()
print('snapshot', len(j), j[:2]==b'\xff\xd8')
open('/home/ifit/xjh/pad_tests/live_real.jpg','wb').write(j)
stop=True; os._exit(0)
