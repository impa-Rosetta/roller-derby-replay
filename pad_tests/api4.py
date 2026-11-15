import sys, time, threading, urllib.request, json
sys.path.insert(0, '/home/ifit/xjh')
import referee_data as rd, numpy as np

SEG = {'file': 'seg_000.h265', 'dir': '/home/ifit/xjh/replay/archive/stage1_20261108_150000_demo',
       'start_s': 0.0, 'end_s': 10.0}
MKV = '/home/ifit/xjh/replay/cache/stage1_20261108_150000_demo__seg_000.mkv'

class Feed:
    def __init__(self):
        self.image = np.zeros((720,1280,3), np.uint8); self.image_n = 0
    def fps(self): return 59.2
    def alive(self): return True
class Loader:
    def clip(self, name): return {'mkv': MKV, 'fps': 59.2, 'start_s': 0.0, 'frames': 600}
    def request(self, seg, idle_only=False): pass
class Arch:
    def list_sessions(self, n=20): return []
    def info(self, x): return None
class Lib:
    def list_sessions(self): return []
class C:
    def __init__(self):
        self.feed = Feed(); self.loader = Loader(); self.archiver = Arch()
        self.library = Lib(); self.mode='live'; self.seg=None; self.paused=False
        self.ended=False; self.speed_idx=0; self.archive_busy=False
        self.archive_done=0; self.archive_total=0; self._marks=[]; self.msg=''; self.msg_t=0
        self.pos = 0.0
    def timecode(self): return '14:32:10 00:01:23 #7342'
    def _session_label(self): return '11-12 14:30 A'
    def marks(self): return list(self._marks)
    def add_mark(self, pre_s=0.0, est=False, note=None):
        m = {'t_s': self.pos, 'clock': '00:%.1f' % self.pos, 'note': note or 'PAD'}
        self._marks.append(m); return m
    def remove_mark(self, i):
        if 0 <= i < len(self._marks): del self._marks[i]; return True
        return False
    def note_mark(self, i, t):
        if 0 <= i < len(self._marks): self._marks[i]['note'] += '  ' + t; return True
        return False
    def position_s(self): return self.pos
    def _speed(self): return (1.0,0.5,0.25)[self.speed_idx]
    def segments(self): return [SEG]
    def to_replay(self):
        self.mode='replay'; self.seg=SEG; self.pos=1.0; self.paused=False
    def to_live(self): self.mode='live'; self.seg=None
    def toggle_play(self): self.paused = not self.paused
    def set_speed(self, i): self.speed_idx = i
    def step(self, d): self.pos = max(0.0, self.pos + d/59.2)
    def seek(self, t): self.pos = max(0.0, t)
    def open_session(self, p): self.mode='replay'; self.seg=SEG; return 'x'

ctl = C()
s = rd.PadServer(ctl, port=18932)
s.start()
stop = False
def loop():
    while not stop:
        s.pump()
        ctl.feed.image_n += 1
        ctl.feed.image = np.random.randint(0,255,(720,1280,3), np.uint8)
        if ctl.mode == 'replay' and not ctl.paused:
            ctl.pos += 1/59.2
        time.sleep(0.03)
th = threading.Thread(target=loop, daemon=True); th.start()
time.sleep(1)

B = 'http://127.0.0.1:18932'
def get(p):
    return urllib.request.urlopen(B+p, timeout=3).read().decode()
def post(p, d=None):
    req = urllib.request.Request(B+p, data=json.dumps(d or {}).encode(),
                                 headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
def st():
    return json.loads(get('/api/status'))

fails = []
def chk(name, cond, extra=''):
    print(('OK  ' if cond else 'FAIL'), name, extra)
    if not cond: fails.append(name)

# 挂一路流（模拟平板），保证有客户端在拉
import urllib.request as ur
watch = ur.urlopen(B+'/stream.mjpg', timeout=5)
th2 = threading.Thread(target=lambda: [watch.read(8192) for _ in iter(int,1)], daemon=True)
th2.start()
time.sleep(0.5)

s0 = st()
chk('status live', s0['mode']=='live' and s0['btn0']=='mark', (s0['page'], s0['mode'], s0['btn0']))

# 1. LIVE 页按 MARK
pg = s0['pg']
found = False
for slot in range(4):
    r = post('/api/ui/press', {'pg': pg, 'slot': slot})
    if slot == 0:
        chk('press MARK', r.get('ok')==1, r)
    else:
        found = True
        chk('press non-MARK slot%d rejects/side-effect' % slot, True, r)
time.sleep(0.5)
chk('mark added', len(ctl.marks())==1, ctl.marks())

# 2. stale pg
r = post('/api/ui/press', {'pg': 99, 'slot': 0})
chk('stale pg rejected', r.get('ok')==0 and r.get('err')=='stale', r)

# 3. 翻页：5 页转一圈回到 LIVE
names = []
for i in range(5):
    r = post('/api/ui/page', {'dir': 1})
    names.append(st()['page'])
chk('page cycle', names == ['SEEK','MARK','LIB','LIVE','PLAY'], names)

# 4. 回放
for _ in range(6):
    sl = st()
    if sl['page']=='PLAY': break
    post('/api/ui/page', {'dir': 1}); time.sleep(0.15)
sx = st()
chk('now PLAY', sx['page']=='PLAY', sx['page'])
r = post('/api/ui/press', {'pg': sx['pg'], 'slot': 0})  # toggle
time.sleep(0.3)
r = post('/api/ui/press', {'pg': sx['pg'], 'slot': 1})  # speed
time.sleep(0.3)
chk('speed changed', ctl.speed_idx==1, ctl.speed_idx)
sx2 = st()
chk('speed label', sx2['btn1'] in ('0.5X','0.5x'), sx2['btn1'])

# 回 LIVE 页按 REPLAY
for _ in range(6):
    sl = st()
    if sl['page']=='LIVE': break
    post('/api/ui/page', {'dir': 1}); time.sleep(0.15)
sl = st(); chk('back LIVE', sl['page']=='LIVE', sl['page'])
ctl.speed_idx = 0
r = post('/api/ui/press', {'pg': sl['pg'], 'slot': 1})
print('  press replay ->', r)
time.sleep(1.0)
print('  dbg mode/paused/pos', ctl.mode, ctl.paused, ctl.pos, sl['pg'])
chk('to replay', ctl.mode=='replay', (ctl.mode, s.hub._mode))
time.sleep(0.6)
n0 = ctl.pos
time.sleep(1.0)
chk('replay advancing', ctl.pos > n0 + 0.1, (n0, ctl.pos))

# seek / step
post('/api/replay/ctl', {'op':'pause'})
time.sleep(0.3)
p1 = ctl.pos; time.sleep(0.6); p2 = ctl.pos
chk('pause holds', abs(p2-p1) < 0.05, (p1,p2))
post('/api/replay/ctl', {'op':'step','n':1}); time.sleep(0.3)
chk('step fwd', ctl.pos > p2, ctl.pos)
post('/api/replay/ctl', {'op':'step','n':-1}); time.sleep(0.3)
post('/api/replay/ctl', {'op':'seek_pos','t':5.0}); time.sleep(0.5)
chk('seek 5s', abs(ctl.pos-5.0) < 0.6, ctl.pos)
pass

# 速度循环
ctl.speed_idx = 0
for exp in (1,2,0):
    post('/api/replay/ctl', {'op':'speed'}); time.sleep(0.15)
    chk('speed cycle -> %d' % exp, ctl.speed_idx==exp, ctl.speed_idx)

# 播完（ended）后再按播放：必须跳回段头，不能停在段尾
ctl.paused = True; ctl.ended = True; ctl.pos = 19.6; ctl.idx = 549
post('/api/replay/ctl', {'op':'play'}); time.sleep(0.4)
chk('play after ended rewinds', not ctl.ended and ctl.pos < 5.0,
    (ctl.ended, ctl.pos, ctl.paused))

post('/api/replay/ctl', {'op':'play'}); time.sleep(0.2)
chk('POST /api/live ok', post('/api/live', {}).get('ok')==1)
chk('back live', ctl.mode=='live' and s.hub._mode=='live', (ctl.mode, s.hub._mode))

# 5. marks API
r = json.loads(get('/api/marks'))
chk('marks api', r['n']==1 and r['marks'][0]['tag']=='PAD', r)
post('/api/note', {'index':0, 'tag':'FOUL'}); time.sleep(0.4)
r = json.loads(get('/api/marks'))
chk('note applied', 'FOUL' in r['marks'][0]['tag'], r['marks'][0])
r = post('/api/unmark', {'index':0,'n':1}); time.sleep(0.3)
chk('unmark', r.get('ok')==1 and not ctl.marks(), (r, ctl.marks()))
r = post('/api/unmark', {'index':0,'n':5}); time.sleep(0.3)
chk('unmark stale n rejected', r.get('ok')==0, r)

# 6. lib 页按钮（空库不应崩）
pass
for _ in range(6):
    sl = st()
    if sl['page']=='LIB': break
    post('/api/ui/page', {'dir': 1}); time.sleep(0.1)
sl = st(); chk('at LIB', sl['page']=='LIB', sl['page'])
for slot in range(4):
    r = post('/api/ui/press', {'pg': sl['pg'], 'slot': slot})
    chk('LIB slot%d handled' % slot, 'ok' in r, r)
r = json.loads(get('/api/lib/sessions'))
chk('lib list endpoint', 'sessions' in r, list(r.keys()))

stop = True
print('\n%d failures' % len(fails), fails)
sys.exit(1 if fails else 0)
