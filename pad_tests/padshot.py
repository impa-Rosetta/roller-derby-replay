#!/usr/bin/env python3
"""离线复刻平板横屏界面，出预览图。

和固件 main.c 同一套几何/配色/字库，逐像素模拟：
  1. 起一个真 PadServer（假 ctl，直播画面用 pad_live.jpg 造）
  2. 按 /api/hud 的字段画 1280x800 逻辑画布
  3. 逆时针转 90° 得到物理 800x1280 的样子（板子横着拿时看到的）
用法：python3 padshot.py [输出路径]
"""
import os
import sys
import time
import json
import types
import urllib.request
import threading
import re

import numpy as np
import cv2

sys.path.insert(0, '/home/ifit/xjh')
import studio_app as sa
import referee_data as rd

SRC = '/home/ifit/esp/ws-p4/examples/esp-idf/13_judge_viewer/main/main.c'
_src = open(SRC).read()
_order = re.search(r'FONT_ORDER\[\] = "([^"]*)"', _src).group(1)
_body = re.search(r'static const uint16_t FONT\[\]\[7\] = \{(.*?)\n\};', _src,
                  re.S).group(1)
_rows = re.findall(r'\{([^}]*)\}', _body)
FONT = {c: [int(x, 16) for x in v.split(',')] for c, v in zip(_order, _rows)}

# 从 main.c 的 #define 里抠几何尺寸，改布局时预览自动跟上
def dfn(name):
    m = re.search(r'#define %s\s+(.+)' % name, _src)
    return m.group(1).split('/*')[0].strip()

CW, CH = 1280, 800
HDR_H, BAR_H, TX_W, BTN_TOP = 72, 112, 144, CH - 112
BTN_GAP, BTN_X0 = 10, TX_W + 12
BTN_X1 = CW - 16
BTN_W = (BTN_X1 - BTN_X0 - BTN_GAP * 3) // 4
PAD_W, PAD_H = 960, 540
SCREEN_W = 1280
VID_X = (SCREEN_W - PAD_W) // 2
VID_Y = HDR_H + (CH - HDR_H - BAR_H - PAD_H) // 2
RP_X = VID_X + PAD_W + 12
RP_Y = HDR_H
RP_W = CW - RP_X - 12
RP_H = CH - HDR_H - BAR_H - 12
RP_LINE = 46
TL_X0, TL_Y, TL_H = 8, CH - 30, 14
TL_X1 = TX_W - 8

def c565(h):
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)

# main.c 里的 RGB565 常量 -> BGR（预览图按 BGR 存）
def bgr(v):
    return ((v & 0x1F) << 3, ((v >> 5) & 0x3F) << 2, ((v >> 11) & 0x1F) << 3)

COL = {}
for n in ('BG', 'PANEL', 'RAISED', 'HOVER', 'BORDER', 'BORDER_L', 'TEXT',
          'MUTED', 'DIM', 'GREEN', 'RED', 'AMBER', 'BLUE', 'PURPLE',
          'BLUE_D', 'GREEN_D', 'RED_D', 'PURPLE_D', 'BLACK'):
    v = int(re.search(r'#define C_%s\s+(0x[0-9A-Fa-f]+)' % n, _src).group(1), 16)
    COL[n] = bgr(v)

GW, GH = 7, 10
def tw(scale): return (GW + 1) * scale
def th(scale): return GH * scale

def glyph_idx(ch):
    ch = ch.upper()
    return _order.index(ch) if ch in _order else -1

def text(img, x, y, s, scale, c):
    col = COL.get(c, c) if isinstance(c, str) else c
    for ch in s:
        gi = glyph_idx(ch)
        if gi >= 0:
            for cx in range(GW):
                bits = FONT[_order[gi]][cx]
                for ry in range(GH):
                    if bits >> ry & 1:
                        yy, xx = y + ry * scale, x + cx * scale
                        if yy + scale <= img.shape[0] and xx + scale <= img.shape[1] and yy >= 0 and xx >= 0:
                            img[yy:yy + scale, xx:xx + scale] = col
        x += tw(scale)

def text_r(img, xr, y, s, scale, c):
    text(img, xr - len(s) * tw(scale), y, s, scale, c)

def text_c(img, x0, x1, y, s, scale, c):
    text(img, x0 + ((x1 - x0) - len(s) * tw(scale)) // 2, y, s, scale, c)

def fill(img, x0, y0, x1, y1, c):
    col = COL.get(c, c) if isinstance(c, str) else c
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.shape[1], x1), min(img.shape[0], y1)
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = col

def round_rect(img, x0, y0, x1, y1, r, fill_c, border_c):
    fc = COL.get(fill_c, fill_c)
    bc = COL.get(border_c, border_c) if border_c else None
    fill(img, x0 + r, y0, x1 - r, y1, fc)
    fill(img, x0, y0 + r, x0 + r, y1 - r, fc)
    fill(img, x1 - r, y0 + r, x1, y1 - r, fc)
    for i in range(r):
        dy = r - i
        inset = r - int(round(np.sqrt(max(0, r * r - dy * dy))))
        fill(img, x0 + inset, y0 + i, x0 + r, y0 + i + 1, fc)
        fill(img, x1 - r, y0 + i, x1 - inset, y0 + i + 1, fc)
        fill(img, x0 + inset, y1 - i - 1, x0 + r, y1 - i, fc)
        fill(img, x1 - r, y1 - i - 1, x1 - inset, y1 - i, fc)
    if bc:
        fill(img, x0 + r, y0, x1 - r, y0 + 2, bc)
        fill(img, x0 + r, y1 - 2, x1 - r, y1, bc)
        fill(img, x0, y0 + r, x0 + 2, y1 - r, bc)
        fill(img, x1 - 2, y0 + r, x1, y1 - r, bc)


def fit(_, s, pw):
    """按像素宽 pw 截断：最多能放下几个字。"""
    n = max(0, pw // tw(2))
    if len(s) > n:
        s = s[:max(0, n - 1)] + '~'
    return s


def render(h, frame):
    img = np.zeros((CH, CW, 3), np.uint8)
    img[:] = COL['BG']
    # 视频
    if frame is not None:
        img[VID_Y:VID_Y + PAD_H, VID_X:VID_X + PAD_W] = frame

    # 顶部
    fill(img, 0, 0, CW, HDR_H, 'PANEL')
    fill(img, 0, HDR_H - 1, CW, HDR_H, 'BORDER')
    live = h['mode'] == 'live'
    x, cy = 16, HDR_H // 2
    md = 'LIVE' if live else 'PLAY'
    fill(img, x, cy - 6, x + 12, cy + 6, COL['GREEN' if live else 'PURPLE'])
    x += 22
    text(img, x, cy - th(3) // 2, md, 3, COL['GREEN' if live else 'PURPLE'])
    x += len(md) * tw(3) + 14
    # 时间码右对齐先占位，剩下的才是左栏
    tc = h.get('timecode') or 'WAIT'
    xr = CW - 16 - len(tc) * tw(5) - 20
    if h.get('info') and x + len(h['info']) * tw(2) <= xr:
        text(img, x, cy - th(2) // 2, h['info'], 2, COL['MUTED'])
    text_r(img, CW - 16, cy - th(5) // 2, tc, 5, COL['TEXT'])
    if h.get('sess_label'):
        lb = 'SESS ' + h['sess_label']
        dur = int(h.get('dur', 0))
        ds = '%02d:%02d' % (dur // 60, dur % 60)
        lx = x + len(h.get('info') or '') * tw(2) + 16
        if lx + len(lb) * tw(2) <= xr - 16:
            text(img, lx, cy - th(2) - 2, lb, 2, COL['MUTED'])
            text(img, lx, cy + 2, ds, 2, COL['DIM'])
    if h.get('msg'):
        text(img, 16, HDR_H - th(2) - 4, fit(0, h['msg'], 420), 2,
             COL['AMBER'])

    # 底部按钮
    fill(img, 0, BTN_TOP, CW, CH, 'PANEL')
    fill(img, 0, BTN_TOP, CW, BTN_TOP + 1, 'BORDER')
    pgs = h['page']
    nms = '%d/%d' % (h['pg'] + 1, h['pn'])
    lx = (BTN_X0 - len(pgs) * tw(2) - len(nms) * tw(2) - 8) // 2
    text(img, lx, BTN_TOP + 12, pgs, 2, COL['MUTED'])
    text(img, lx + len(pgs) * tw(2) + 8, BTN_TOP + 12, nms, 2, COL['DIM'])
    for i in range(4):
        x0 = BTN_X0 + i * (BTN_W + BTN_GAP)
        x1 = x0 + BTN_W
        y0, y1 = BTN_TOP + 12, CH - 12
        lab = h['btn%d' % i]
        if not lab:
            round_rect(img, x0, y0, x1, y1, 8, 'BG', None)
            continue
        on = h['hl'][i] == '1'
        fill_c = COL['BLUE_D'] if on else COL['RAISED']
        bord = COL['BLUE'] if on else COL['BORDER_L']
        if lab.upper() in ('MARK', 'NOTE'):
            fill_c, bord = COL['PURPLE_D'], COL['PURPLE']
        elif lab.upper() == 'LIVE':
            fill_c, bord = COL['GREEN_D'], COL['GREEN']
        elif lab.upper() == 'DEL':
            fill_c, bord = COL['RED_D'], COL['RED']
        round_rect(img, x0, y0, x1, y1, 8, fill_c, bord)
        text_c(img, x0, x1, y0 + (y1 - y0 - th(3)) // 2, lab, 3, COL['TEXT'])

    # 右侧面板
    fill(img, RP_X, RP_Y, RP_X + RP_W, RP_Y + RP_H, 'PANEL')
    fill(img, RP_X, RP_Y, RP_X + RP_W, RP_Y + 1, 'BORDER')
    fill(img, RP_X, RP_Y, RP_X + 1, RP_Y + RP_H, 'BORDER')
    is_lib = h['page'] == 'LIB'
    head = ('LIB %d' % len(h.get('lib', []))) if is_lib else (
        'MARKS %d' % h.get('mark_count', 0))
    text(img, RP_X + 10, RP_Y + 8, head, 2, COL['MUTED'])
    fill(img, RP_X + 10, RP_Y + 34, RP_X + RP_W - 10, RP_Y + 35, 'BORDER')
    y = RP_Y + 42
    if is_lib:
        for i, s in enumerate(h.get('lib', [])):
            if y + RP_LINE > RP_Y + RP_H:
                break
            on = i == h.get('lib_sel', 0)
            if on:
                fill(img, RP_X, y, RP_X + RP_W, y + RP_LINE, 'BLUE_D')
            text(img, RP_X + 10, y + 3, s['name'], 2,
                 COL['TEXT'] if on else COL['MUTED'])
            sub = '%02d:%02d %dMB %dMK' % (s['sec'] // 60, s['sec'] % 60,
                                           s['mb'], s['marks'])
            text(img, RP_X + 10, y + 23, sub, 2,
                 COL['TEXT'] if on else COL['DIM'])
            y += RP_LINE
        if not h.get('lib'):
            text(img, RP_X + 10, RP_Y + 60, 'NO SESSION', 2, COL['DIM'])
    else:
        for i, m in enumerate(h.get('marks', [])):
            if y + RP_LINE > RP_Y + RP_H:
                break
            fill(img, RP_X, y, RP_X + RP_W, y + RP_LINE - 1,
                 'RAISED' if i & 1 else 'PANEL')
            fill(img, RP_X + 8, y + 10, RP_X + 16, y + 18, 'PURPLE')
            text(img, RP_X + 22, y + 6, '#%d' % i, 2, COL['MUTED'])
            tm = m['t'] or '--:--'
            text(img, RP_X + 22, y + 26, tm, 2, COL['TEXT'])
            tx1 = RP_X + 22 + len(tm) * tw(2)
            n = (RP_X + RP_W - 8 - tx1 - 8) // tw(2)
            if m['tag'] and n > 0:
                text(img, tx1 + 8, y + 26, m['tag'][:n], 2, COL['AMBER'])
            y += RP_LINE
        if not h.get('marks'):
            text(img, RP_X + 10, RP_Y + 60, 'NO MARK', 2, COL['DIM'])

    # 左下时间轴
    fill(img, 0, BTN_TOP, TX_W, CH, 'PANEL')
    fill(img, 0, BTN_TOP, TX_W, BTN_TOP + 1, 'BORDER')
    fill(img, TL_X0, TL_Y, TL_X1, TL_Y + TL_H, 'BG')
    t0, t1 = h.get('t0', 0.0), max(h.get('t1', 1.0), h.get('t0', 0.0) + 1.0)
    span = max(1.0, t1 - t0)
    wpx = TL_X1 - TL_X0
    for i, (a, b) in enumerate(h.get('segs', [])):
        xa = TL_X0 + int((a - t0) / span * wpx)
        xb = TL_X0 + int((b - t0) / span * wpx)
        if xb > xa:
            fill(img, xa, TL_Y + 2, xb, TL_Y + TL_H - 2,
                 'RAISED' if i & 1 else 'HOVER')
    for m in h.get('marks', []):
        if t0 <= m['t_s'] <= t1:
            mx = TL_X0 + int((m['t_s'] - t0) / span * wpx)
            fill(img, mx, TL_Y, mx + 3, TL_Y + TL_H, 'PURPLE')
    pos = min(max(h.get('pos', 0.0), t0), t1)
    px = TL_X0 + int((pos - t0) / span * wpx)
    fill(img, px - 1, TL_Y - 4, px + 2, TL_Y + TL_H + 4, 'BLUE')
    a_s = '%02d:%02d' % (int(h.get('pos', 0)) // 60, int(h.get('pos', 0)) % 60)
    b_s = '%02d:%02d' % (int(h.get('dur', 0)) // 60, int(h.get('dur', 0)) % 60)
    ty = TL_Y - th(2) - 1
    text(img, TL_X0, ty, a_s, 2, COL['TEXT'])
    if len(a_s) * tw(2) + len(b_s) * tw(2) + 6 <= TL_X1 - TL_X0:
        text_r(img, TL_X1, ty, b_s, 2, COL['MUTED'])
    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else '/home/ifit/xjh/pad_tests/pad_ui_land.png'
    args = types.SimpleNamespace(
        host='127.0.0.1', live_port=8601, thumb_port=0, display_width=800,
        out_dir='/home/ifit/xjh/replay/archive',
        cache_dir='/home/ifit/xjh/replay/cache', cache_max_gb=50,
        remote=None, remote_root='/home/syd/var_replay',
        archive_interval=3.0, rec_segment=10.0, rec_ring=90.0,
        rec_min_free_gb=1.0, speed=0, referee_port=0)
    ctl = sa.StudioController(args)
    ctl.timecode = lambda: '14:32:10 00:01:23 #7342'
    ctl._session_label = lambda: 'STAGE1 11-12'
    ctl.feed.fps = lambda: 59.2
    ctl.feed.alive = lambda: True
    ctl.position_s = lambda: 83.4
    ctl.timeline_end = lambda: 610.0
    ctl.segments_window = lambda span: ([
        {'start_s': 0.0, 'end_s': 200.0}, {'start_s': 200.0, 'end_s': 400.0},
        {'start_s': 400.0, 'end_s': 610.0}], 0.0)
    ctl.segments = lambda: [{'start_s': 0.0, 'end_s': 610.0}]
    srv = rd.PadServer(ctl, port=int(os.environ.get('PP', '18920')))
    srv.lib_sessions = lambda: [
        {'name': 'stage1_1120_1430', 'seconds': 610, 'bytes': 138412032,
         'marks': 3},
        {'name': 'stage1_1120_1600', 'seconds': 480, 'bytes': 99614720,
         'marks': 0},
    ]
    srv.start()

    frame = cv2.resize(cv2.imread('/home/ifit/xjh/pad_tests/pad_live.jpg'),
                       (PAD_W, PAD_H))
    cv2.circle(frame, (PAD_W // 2, PAD_H - 120), 60, (40, 200, 40), -1)
    stop = [False]

    def loop():
        while not stop[0]:
            ctl.feed.image = frame
            ctl.feed.image_n += 1
            srv.hub.tick()
            srv.pump()
            time.sleep(0.04)
    threading.Thread(target=loop, daemon=True).start()

    base = 'http://127.0.0.1:%d' % srv.port
    def post(path, obj):
        r = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                   headers={'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(r).read())

    shots = []
    def grab(tag):
        st = json.loads(urllib.request.urlopen(base + '/api/hud').read())
        img = render(st, frame)
        # 物理屏上的样子：逆时针 90° -> 800x1280
        phys = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        shots.append((tag, phys))
        print(tag, {k: st[k] for k in ('page', 'btn0', 'btn1', 'btn2', 'btn3',
                                       'hl', 'info', 'mark_count')})
        return st

    def nav(name, tries=6):
        for _ in range(tries):
            st = json.loads(urllib.request.urlopen(base + '/api/hud').read())
            if st['page'] == name:
                return st
            post('/api/ui/page', {'dir': 1})
            time.sleep(0.3)
        return st

    time.sleep(1.2)
    grab('LIVE')
    for i in range(3):
        post('/api/ui/press', {'pg': 0, 'slot': 0})
        time.sleep(0.3)
    grab('LIVE+MARK')
    nav('LIB')
    grab('LIB')
    nav('PLAY')
    grab('PLAY')
    nav('SEEK')
    grab('SEEK')
    nav('MARK')
    grab('MARK')
    stop[0] = True

    outimg = np.hstack([cv2.resize(im, (400, 640)) for _, im in shots])
    cv2.imwrite(out, outimg)
    print('wrote', out, outimg.shape)
    os._exit(0)


if __name__ == '__main__':
    main()
