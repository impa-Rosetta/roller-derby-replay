import sys, time, threading, json, urllib.request
sys.path.insert(0, '/home/ifit/xjh')
import referee_data as rd, studio_services as svc, numpy as np
ARCH = '/home/ifit/xjh/replay/archive'
class F:
    def __init__(self):
        self.image = np.zeros((720,1280,3), np.uint8); self.image_n = 0
    def fps(self): return 59.2
    def alive(self): return True
class C:
    def __init__(self):
        self.feed = F(); self.mode='live'; self.seg=None; self.paused=False
        self.ended=False; self.speed_idx=0; self.pos=0.0
        self.archive_busy=False; self.archive_done=0; self.archive_total=0
        self._marks=[]; self.msg=''; self.msg_t=0
        self.opened=None
        self.library = svc.Library([ARCH], marks_path='/home/ifit/xjh/replay/marks.jsonl')
        self.archiver = type('A',(),{'list_sessions':lambda s,n=20: [], 'info': lambda s,x: None})()
        self.loader = svc.ClipLoader('jetson', '/home/ifit/xjh/replay/cache', 20.0, local_dirs=[ARCH])
        self.loader.start()
    def timecode(self): return 'T'
    def _session_label(self): return 'S'
    def marks(self): return []
    def position_s(self): return self.pos
    def _speed(self): return 1.0
    def segments(self): return []
    def to_live(self): self.mode='live'
    def to_replay(self): pass
    def seek(self, t): self.pos = max(0.0, t)
    def toggle_play(self): self.paused = not self.paused
    def step(self, d): pass
    def set_speed(self, i): self.speed_idx = i
    def open_session(self, p):
        r = svc.read_session_dir(p)
        print('read_session_dir:', None if r is None else (r[0], len(r[1])))
        if r is None: return None
        name, segs, base = r
        self.opened = {'name': name, 'root': ARCH, 'dir': p, 'segs': segs, 'base': base}
        self.segs = segs; self.mode='replay'; self.seg=segs[0]; self.pos=0.5
        return name
ctl = C()
s = rd.PadServer(ctl, port=18781); s.start()
def loop():
    while True:
        s.pump(); ctl.feed.image_n += 1
        ctl.feed.image = np.random.randint(0,255,(720,1280,3), np.uint8); time.sleep(0.03)
threading.Thread(target=loop, daemon=True).start(); time.sleep(1)
def post(p,b):
    r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18781'+p,data=json.dumps(b).encode(),method='POST'),timeout=10)
    return json.loads(r.read())
def get(p): return json.loads(urllib.request.urlopen('http://127.0.0.1:18781'+p,timeout=10).read())
sess = get('/api/lib/sessions')
print('sessions', [(x['name'], x['sec'], x['mb'], x['marks'], x['src']) for x in sess['sessions']])
print('open', post('/api/replay/open', {'session': sess['sessions'][0]['name']}), 'mode', ctl.mode, 'seg', ctl.seg['file'])
time.sleep(3)
print('holder seq', s.hub.pad.seq, 'replay seq', s.hub.replay.seq)
r = urllib.request.urlopen('http://127.0.0.1:18781/stream.mjpg', timeout=5)
d=b''; t0=time.time()
while time.time()-t0 < 3: d += r.read(8192)
r.close()
print('stream frames', d.count(b'\xff\xd8'), 'bytes', len(d))
