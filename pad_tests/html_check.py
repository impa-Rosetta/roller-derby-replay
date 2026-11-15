import sys, os, time, threading, types, urllib.request
sys.path.insert(0, '/home/ifit/xjh')
import studio_app as sa, referee_data as rd, numpy as np, cv2
args = types.SimpleNamespace(host='127.0.0.1', live_port=8601, thumb_port=0, display_width=800,
    out_dir='/home/ifit/xjh/replay/archive', cache_dir='/home/ifit/xjh/replay/cache',
    cache_max_gb=50, remote=None, remote_root='/home/syd/var_replay',
    archive_interval=3.0, rec_segment=10.0, rec_ring=90.0, rec_min_free_gb=1.0, speed=0, referee_port=0)
ctl = sa.StudioController(args)
srv = rd.PadServer(ctl, port=int(os.environ.get('PP','18940'))); srv.start()
frame = cv2.resize(cv2.imread('/home/ifit/xjh/pad_tests/pad_live.jpg'),(800,1280))
stop=False
def loop():
    while not stop:
        ctl.feed.image=frame; ctl.feed.image_n+=1; srv.hub.tick(); srv.pump(); time.sleep(0.05)
threading.Thread(target=loop, daemon=True).start(); time.sleep(1)
B='http://127.0.0.1:%d'%srv.port
html = urllib.request.urlopen(B+'/').read().decode()
print('html bytes', len(html), 'refs live.mjpg:', 'live.mjpg' in html, 'stream.mjpg:', 'stream.mjpg' in html)
for p in ['/live.mjpg','/replay.mjpg','/stream.mjpg']:
    try:
        r=urllib.request.urlopen(B+p, timeout=3); d=r.read(20000)
        print(p, 'first bytes', d[:20][:20], 'is multipart', b'--frame' in d)
    except Exception as e:
        print(p, 'ERR', type(e).__name__, e)
stop=True; os._exit(0)
