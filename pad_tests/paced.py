import sys, os, time, urllib.request, types, json, threading, socket
sys.path.insert(0, '/home/ifit/xjh')
import studio_app as sa, referee_data as rd, numpy as np, cv2
args = types.SimpleNamespace(host='127.0.0.1', live_port=8601, thumb_port=0, display_width=800,
    out_dir='/home/ifit/xjh/replay/archive', cache_dir='/home/ifit/xjh/replay/cache',
    cache_max_gb=50, remote=None, remote_root='/home/syd/var_replay',
    archive_interval=3.0, rec_segment=10.0, rec_ring=90.0, rec_min_free_gb=1.0,
    speed=1.0, referee_port=0)
ctl = sa.StudioController(args)
srv = rd.PadServer(ctl, port=int(os.environ.get('PP','18900')))
srv.start()
base = cv2.resize(cv2.imread('/home/ifit/xjh/pad_tests/pad_live.jpg'), (800,1280))
stop=False
def loop():
    i=0
    while not stop:
        f = base.copy()
        cv2.putText(f, 'frame%d' % i, (40, 700), cv2.FONT_HERSHEY_SIMPLEX, 4, (255,255,255), 8)
        ctl.feed.image = f; ctl.feed.image_n += 1
        srv.hub.tick(); srv.pump()
        i+=1; time.sleep(1/30)
threading.Thread(target=loop, daemon=True).start()
time.sleep(1.5)
B='http://127.0.0.1:%d/stream.mjpg' % srv.port
sock = socket.create_connection(('127.0.0.1', srv.port)); sock.settimeout(3)
sock.sendall(b'GET /stream.mjpg HTTP/1.0\r\nHost: x\r\n\r\n')
d=b''; t0=time.time()
while time.time()-t0<4:
    try: d += sock.recv(65536)
    except socket.timeout: break
dt = time.time()-t0
print('live frames %d in %.1fs -> %.1f fps, bytes %d' % (d.count(b'\xff\xd8'), dt, d.count(b'\xff\xd8')/dt, len(d)), flush=True)
sock.close()
# 统计编码日志
stop=True; os._exit(0)
