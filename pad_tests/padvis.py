import sys, os, time, urllib.request, types, json, threading
sys.path.insert(0, '/home/ifit/xjh')
import studio_app as sa, referee_data as rd, numpy as np, cv2

# ---- 与 main.c 的 cv_fill / cv_text / draw_bar / sub_rebuild / osd_rebuild 同构 ----
import re as _re
from PIL import Image, ImageDraw
_src=open('/home/ifit/esp/ws-p4/examples/esp-idf/13_judge_viewer/main/main.c').read()
_order=_re.search(r'FONT_ORDER\[\] = "([^"]*)"',_src).group(1)
_body=_re.search(r'static const uint8_t FONT\[\]\[5\] = \{(.*?)\n\};',_src,_re.S).group(1)
_rows=_re.findall(r'\{([^}]*)\}',_body)
FONT={c:[int(x,16) for x in v.split(',')] for c,v in zip(_order,_rows)}

W, H = 800, 1280
OSD_H, OSD_SUB_H, BAR_H, BAR_TITLE_H, BTN_W, BTN_GAP, BTN_H = 56, 34, 96, 20, 182, 8, 70
C_BG, C_PANEL, C_TEXT, C_DIM, C_LIVE, C_BTN, C_HL = (8,8,8),(24,24,24),(255,255,255),(131,129,131),(41,206,65),(32,32,32),(180,36,32)
def fw_txt(img,x,y,s,scale,c):
    c=(c,c,c) if not isinstance(c,tuple) else c
    for ch in s:
        g=FONT.get(ch.upper())
        if g:
            for cx in range(5):
                for ry in range(7):
                    if g[cx]>>ry&1:
                        img[y+ry*scale:y+ry*scale+scale, x+cx*scale:x+cx*scale+scale] = c
        x+=6*scale
def rgb(c): return (c[0]//255, c[1]//255, c[2]//255)
def fill(img,x0,y0,x1,y1,c):
    c=(c,c,c) if not isinstance(c,tuple) else c
    img[max(0,y0):max(0,y1), max(0,x0):max(0,x1)] = c
def text(img,x,y,s,scale,c):
    fw_txt(img,x,y,s,scale,c)
def draw_bar(img, st):
    fill(img,0,H-BAR_H,W,H,C_PANEL)
    text(img,12,H-BAR_H+3,'%s  P%d/%d'%(st['page'],st['pg']+1,st['pn']),2,C_DIM)
    x=(W-(BTN_W*4+BTN_GAP*3))//2; y=H-BAR_H+BAR_TITLE_H+2
    for i in range(4):
        lab=st['btn%d'%i]
        if not lab:
            fill(img,x,y,x+BTN_W,y+BTN_H,C_PANEL)
        else:
            fill(img,x,y,x+BTN_W,y+BTN_H, C_HL if st['hl'][i]=='1' else C_BTN)
            tw=len(lab)*6*3
            text(img, x+((BTN_W-tw)//2 if tw<=BTN_W-12 else 6), y+(BTN_H-21)//2, lab,3,C_TEXT)
        x+=BTN_W+BTN_GAP
def draw_sub(img, st):
    fill(img,0,OSD_H,W,OSD_H+OSD_SUB_H,C_PANEL)
    if st['sess_label']: text(img,16,OSD_H+6,st['sess_label'],2,C_DIM)
    if st['info']: text(img,W-16-len(st['info'])*12,OSD_H+6,st['info'],2,C_LIVE)
def draw_osd(img, st):
    fill(img,0,0,W,OSD_H,C_PANEL)
    text(img,16,14, st['timecode'] or 'WAIT',3,C_TEXT)
    cnt='#%d'%st['mark_count']
    text(img,W-16-len(cnt)*18,14,cnt,3,C_LIVE)

args = types.SimpleNamespace(host='127.0.0.1', live_port=8601, thumb_port=0, display_width=800,
    out_dir='/home/ifit/xjh/replay/archive', cache_dir='/home/ifit/xjh/replay/cache',
    cache_max_gb=50, remote=None, remote_root='/home/syd/var_replay',
    archive_interval=3.0, rec_segment=10.0, rec_ring=90.0, rec_min_free_gb=1.0,
    speed=0, referee_port=0)
ctl = sa.StudioController(args)
ctl.timecode = lambda: '14:32:10 00:01:23 #7342'
ctl._session_label = lambda: 'STAGE1 11-12 14:30'
ctl.feed.fps = lambda: 59.2
ctl.feed.alive = lambda: True
srv = rd.PadServer(ctl, port=int(os.environ.get('PP','18910')))
srv.start()
frame = cv2.resize(cv2.imread('/home/ifit/xjh/pad_tests/pad_live.jpg'),(800,1280))
# 画面上加个"球"让它像直播
cv2.circle(frame,(400,800),90,(40,200,40),-1)
stop=False
def loop():
    while not stop:
        ctl.feed.image=frame; ctl.feed.image_n+=1
        srv.hub.tick(); srv.pump(); time.sleep(0.04)
threading.Thread(target=loop, daemon=True).start()
B='http://127.0.0.1:%d'%srv.port
shots=[]
def grab(tag):
    st=json.loads(urllib.request.urlopen(B+'/api/status').read())
    j=urllib.request.urlopen(B+'/snapshot.jpg').read()
    img=cv2.imdecode(np.frombuffer(j,np.uint8),1)
    draw_osd(img,st); draw_sub(img,st); draw_bar(img,st)
    shots.append((tag,img)); print(tag, {k:st[k] for k in ('page','btn0','btn1','btn2','btn3','hl','info')})
    return st
time.sleep(1.2)
grab('LIVE')
# 打一条标记
r=urllib.request.Request(B+'/api/ui/press',data=b'{"pg":0,"slot":0}',headers={'Content-Type':'application/json'})
urllib.request.urlopen(r).read(); time.sleep(0.6)
st=grab('LIVE+MARK')
# 翻到 PLAY
urllib.request.urlopen(urllib.request.Request(B+'/api/ui/page',data=b'{"dir":1}',headers={'Content-Type':'application/json'})).read()
time.sleep(0.5); grab('PLAY')
urllib.request.urlopen(urllib.request.Request(B+'/api/ui/page',data=b'{"dir":1}',headers={'Content-Type':'application/json'})).read()
time.sleep(0.5); grab('SEEK')
urllib.request.urlopen(urllib.request.Request(B+'/api/ui/page',data=b'{"dir":1}',headers={'Content-Type':'application/json'})).read()
time.sleep(0.5); grab('MARK')
urllib.request.urlopen(urllib.request.Request(B+'/api/ui/page',data=b'{"dir":1}',headers={'Content-Type':'application/json'})).read()
time.sleep(0.5); grab('LIB')
import numpy as np2
out = np2.hstack([s[1] for s in shots])
out = cv2.resize(out, (out.shape[1]//2, out.shape[0]//2))
cv2.imwrite('/home/ifit/xjh/pad_tests/pad_ui.png', out)
print('wrote /home/ifit/xjh/pad_tests/pad_ui.png', out.shape)
stop=True; os._exit(0)
