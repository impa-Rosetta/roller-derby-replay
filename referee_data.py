#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁判遥控服务（PadServer）：把监看台的状态、画面和操作暴露成 HTTP。

监看台（studio_app.py）的全部状态都在 Tk 主线程的对象上：时间码依赖
feed.image_n、回放依赖 cv2.VideoCapture，都不是线程安全的。所以这里
分成两半：

  * 读：HTTP 线程直接读控制器上**只读**的属性（image/mode/position_s 等，
    赋值本身原子，与 UI 读同一份数据），不碰任何会被 tick 改一半的结构。
  * 写：一律打包成闭包丢进队列，由 Tk 主线程在 pad.pump() 里执行。
    以前 HTTP 线程直接调 ctl.add_mark()，和 tick 并发改 _marks 列表。

画面走三条 MJPEG：
  /stream.mjpg  按当前模式自动选直播或回放，平板只认这个
  /live.mjpg    直播帧（直接复用监看台的 feed.image，不多解一路）
  /replay.mjpg  回放帧（独立 VideoCapture 读 ClipLoader 缓存好的 MKV）
切源时 PadHub 换代号，正在推的流主动断开，平板 1 秒后重连即是新画面。

用法（studio_app.py 里）:
    from referee_data import PadServer
    self.pad = PadServer(self, port=8610)
    self.pad.start()
    # 主循环里 ctl.tick() 之后调 self.pad.pump()

接口:
    GET  /api/status        监看台全部状态 + 当前页按钮（JSON）
    GET  /api/hud           面板化 UI 用：时间轴/标记/场次库（JSON）
    GET  /api/marks         标记列表（JSON）
    POST /api/mark          打一条标记  {"note": "可选", "pre_s": 0}
    POST /api/unmark        删一条标记  {"index": i, "n": 总条数}
    POST /api/note          给标记加备注 {"index": i, "tag": "FOUL"}
    POST /api/ui/page       翻页      {"dir": -1 | 1}
    POST /api/ui/press      按按钮    {"pg": 页代号, "slot": 0..3}
    POST /api/mark/select   点标记列表 {"index": i}
    POST /api/replay/open   打开场次  {"session": "stage1_..."}
    POST /api/replay/ctl    回放控制  {"op": "...", ...}
    POST /api/lib/ctl       录像库    {"op": "prev|next|open|archive"}
    GET  /api/lib/sessions  场次列表（JSON）
    GET  /api/live          切回直播
    GET  /stream.mjpg /live.mjpg /replay.mjpg /snapshot.jpg
"""
import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import cv2
import numpy as np

# 平板的 JPEG 解码缓冲是 320KB（main.c: JPG_MAX_BYTES），超了整帧会被
# 丢掉并断流。留 16KB 余量：固件上限值没同步改时也不会踩线。
JPEG_MAX_BYTES = 300 * 1024
# 物理屏是 800x1280 竖屏，但画面整体横过来用（板子横着拿），逻辑画布
# 1280x800。出图尺寸受 2.4GHz 链路制约：实测有效吞吐 ~8.4Mbps，
# 960x540(x0.104KB/千px) ≈ 54KB/帧，正好压在 20fps 上；
# 1280x720 要 96KB 只能跑 11fps，所以在 OSD 里居中放这块，不铺满。
PAD_W, PAD_H = 960, 540
SCREEN_W, SCREEN_H = 1280, 800
LIVE_FPS = 20.0
REPLAY_FPS = 15.0

# 页面表：(页名, 4 个按钮)。槽位标签就是消息体里的 op，空串 = 灰槽。
PAGES = [
    ('LIVE', ('mark', 'replay', 'lib', '')),
    ('PLAY', ('toggle', 'speed', 'step-1', 'step+1')),
    ('SEEK', ('seek-5', 'seek+5', 'mark<', 'mark>')),
    ('MARK', ('list', 'del', 'note', 'summary')),
    ('LIB', ('prev', 'next', 'open', 'archive')),
]

# 显示名：4 个槽要并排放在 800px 里，长词会缩到画不下（固件会退回
# 靠左画、溢出去）。按钮高亮值和动作无关，只影响这一行字。
LABELS = {
    'step-1': '<1F', 'step+1': '1F>',
    'seek-5': '-5S', 'seek+5': '+5S',
    'mark<': '<MK', 'mark>': 'MK>',
    'replay': 'PLAY', 'toggle': 'GO',
    'open': 'OPEN', 'archive': 'ARC',
    # 平板字库只有 47 个字符，按钮标签限长才能排两行放下
    'list': 'LIST', 'note': 'NOTE', 'summary': 'SUM',
}


def _ascii(s, limit=0):
    """只留 ASCII 可见字符：点阵字库认不出的字符一律变空格。"""
    out = ''.join(c if 32 <= ord(c) < 127 else ' ' for c in (s or ''))
    out = ' '.join(out.split())
    return out[:limit] if limit else out


def _jstr(obj, key, limit=0):
    return _ascii(obj.get(key, ''), limit)


# ---------------- 画面：编码 + 广播 ----------------

class PadStream:
    """一块 JPEG 的最新帧 + 代号。每位 HTTP 客户端各持一个游标。

    推流是"有客户端才编码"：没有人在看就不做 cv2.imencode（CPU 编码
    比 NVDEC 贵得多，没人看时白烧 CPU）。
    """

    def __init__(self):
        self.cv = threading.Condition()
        self.jpg = None
        self.seq = 0
        self.gen = 0          # 换源就 +1，老客户端据此断开重连
        self.clients = 0

    def publish(self, jpg, gen=None):
        with self.cv:
            self.jpg = jpg
            self.seq += 1
            if gen is not None:
                self.gen = gen
            self.cv.notify_all()

    def switch(self, gen):
        """换源：代号变了，正在推的客户端下一轮自己退出。"""
        with self.cv:
            self.jpg = None
            self.gen = gen
            self.cv.notify_all()

    def want(self):
        return self.clients > 0

    def enter(self):
        with self.cv:
            self.clients += 1
            return self.gen

    def leave(self):
        with self.cv:
            self.clients -= 1

    def wait(self, seq, gen, timeout):
        """等到比 seq 新的帧。返回 (jpg, seq)；换源返回 (None, seq)。"""
        with self.cv:
            if gen != self.gen:
                return None, seq
            if self.seq == seq:
                self.cv.wait(timeout)
            if gen != self.gen:
                return None, seq
            return self.jpg, self.seq


def fit_pad(img):
    """把画面填满 960x540 的输出框（cover：保持比例，多出的边裁掉）。

    以前是 contain（缩到框内留黑边），竖屏时源和框比例接近看不出来；
    横屏 16:9 下 4:3 的源会左右留大黑边，裁掉更划算——裁判看的是
    场上动作，宁可切掉天花板的边角也不要一块黑。
    """
    h, w = img.shape[:2]
    if w == PAD_W and h == PAD_H:
        return img
    s = max(PAD_W / float(w), PAD_H / float(h))
    nw, nh = max(PAD_W, int(w * s)), max(PAD_H, int(h * s))
    if nw != w or nh != h:
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    x0 = (nw - PAD_W) // 2
    y0 = (nh - PAD_H) // 2
    return img[y0:y0 + PAD_H, x0:x0 + PAD_W]


def encode_jpg_fast(img, quality=70):
    """只要 ≤320KB 就一次编码到位，不再逐级降质量重编。

    平板推送里 q70 就够（960x540 实测 55KB），而 encode_jpg 逐级
    试 q70→55→40 时每一级都要重新 imencode，超一帧就白烧两倍 CPU。
    """
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    if len(buf) > JPEG_MAX_BYTES:
        ok, buf = cv2.imencode('.jpg', img,
                               [cv2.IMWRITE_JPEG_QUALITY, quality - 30])
        if not ok or len(buf) > JPEG_MAX_BYTES:
            return None
    return buf.tobytes()


_BLANK_JPG = None


def blank_jpg():
    """直播还没画面时的占位帧（编码一次就缓存）。

    固件会把 OSD 叠在这上面；这里不画反馈字——本机没装 CJK 字体时
    cv2.putText 画不出中文，英文又和固件 OSD 重复，画个灰底就够了。
    """
    global _BLANK_JPG
    if _BLANK_JPG is None:
        img = np.full((PAD_H, PAD_W, 3), 24, np.uint8)
        _BLANK_JPG = encode_jpg(img) or b''
    return _BLANK_JPG or None


def encode_jpg(img, quality=70):
    """压到固件收得下的体积。

    纯随机噪声（码流压不动）在 q40 下也过 320KB，那种帧直接丢；
    正常画面 q70 就够，夜景/雪场这类高细节的降到 q55 也行。
    """
    for q in (quality, 55, 40):
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if not ok:
            return None
        if len(buf) <= JPEG_MAX_BYTES:
            return buf.tobytes()
    return None


class _Seq:
    """串行化 cv2.imencode：直播/回放两路 tap 会同时编码。

    imencode 内部开 OpenCV 线程池，两路并发时互相抢核，单帧反而更慢；
    而且帧率低的时候排队也不会积压（编码耗时 < 帧间隔）。
    """
    def __init__(self):
        self.lk = threading.Lock()

    def __enter__(self):
        self.lk.acquire()
        return self

    def __exit__(self, *a):
        self.lk.release()


_ENC_LOCK = _Seq()


def encode_jpg_locked(img, quality=70):
    with _ENC_LOCK:
        return encode_jpg_fast(img, quality)


# ---------------- 直播帧 ----------------

class PadLiveTap(threading.Thread):
    """直接搬监看台已经解好的直播帧，不另开解码器。

    监看台自己就在拉 Jetson 9102 缩略流（LiveFeed），这里只做 JPEG
    编码。handheld_server.py 那套 FrameHub 会另起一路 NVDEC，与其重复
    解同一路流，不如复用内存里现成的那帧。
    """

    def __init__(self, ctl, stream):
        super().__init__(daemon=True)
        self.ctl = ctl
        self.stream = stream
        self.stop_evt = threading.Event()
        # 只有 /live.mjpg（浏览器调试）需要"一直有帧"；/stream.mjpg 走
        # hub 的通道，有人从 switch() 置位后才编码。
        self.always = False
        self._last_n = -1
        self._next_enc = 0.0

    def run(self):
        while not self.stop_evt.is_set():
            if not (self.always or self.stream.want()):
                self.stop_evt.wait(0.2)
                continue
            feed = self.ctl.feed
            img = feed.image
            n = feed.image_n
            if img is None or n == self._last_n:
                self.stop_evt.wait(0.02)
                continue
            now = time.monotonic()
            if now < self._next_enc:
                self.stop_evt.wait(self._next_enc - now)
                continue
            self._next_enc = max(now, self._next_enc + 1.0 / LIVE_FPS)
            try:
                jpg = encode_jpg_locked(fit_pad(img))
            except Exception as e:
                print('直播编码失败: %s: %s' % (type(e).__name__, e),
                      flush=True)
                self.stop_evt.wait(0.2)
                continue
            self._last_n = n
            if jpg is not None:
                self.stream.publish(jpg)


# ---------------- 回放帧 ----------------

class PadReplayTap(threading.Thread):
    """独立读回放片段推给平板。

    绝不动 ctl.cap：那是 Tk 主线程的回放播放器，两边共用一个
    VideoCapture 的当前位置会互相打架。这里自己开一个，只**跟随**
    ctl.seg / ctl.pos_s / ctl.paused / ctl._speed()，偏差超过 0.3 秒
    才 seek，段号变了就重开文件（段推进仍由 ctl.tick 决定）。
    """

    FOLLOW_S = 0.3
    OPEN_TIMEOUT = 3.0

    def __init__(self, ctl, stream):
        super().__init__(daemon=True)
        self.ctl = ctl
        self.stream = stream
        self.stop_evt = threading.Event()
        self.cap = None
        self._seg = None
        self._fps = 59.2
        self._pos = 0.0
        self._next_t = 0.0
        self._wait_req = 0.0
        self.fail = ''

    def _grab_seg(self):
        """当前段对应的 MKV 路径；还没缓存好就顺手请求，返回 None。"""
        ctl = self.ctl
        seg = getattr(ctl, 'seg', None)
        if seg is None:
            self.fail = 'wait_seg'
            return None
        info = ctl.loader.clip(seg['file'])
        if info is None:
            self.fail = 'loading'
            ctl.loader.request(seg)
            return None
        self.fail = ''
        return info

    def _open(self, info, seg, pos_s):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        cap = cv2.VideoCapture(info['mkv'])
        if not cap.isOpened():
            self.cap = None
            self.fail = 'open_fail'
            return False
        self.cap = cap
        self._seg = seg['file']
        self._fps = info.get('fps') or cap.get(cv2.CAP_PROP_FPS) or 59.2
        self._seek(pos_s)
        return True

    def _seek(self, pos_s):
        seg = self.ctl.seg
        if self.cap is None or seg is None:
            return
        f = int(round((pos_s - seg['start_s']) * self._fps))
        f = max(0, f)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        self._pos = pos_s
        self._next_t = 0.0

    def _pub(self, jpg):
        # 回放帧同时喂给 /replay.mjpg 和 /stream.mjpg（PadHub 的反向通道）。
        self.stream.replay.publish(jpg)
        self.stream.pad.publish(jpg, gen=self.stream.gen)

    def run(self):
        ctl = self.ctl
        while not self.stop_evt.is_set():
            if not (self.stream.pad.want() or self.stream.replay.want()):
                self._wait_req = 0.0
                self.stop_evt.wait(0.2)
                continue
            if ctl.mode != 'replay':
                self._wait_req = 0.0
                self.stop_evt.wait(0.1)
                continue
            seg = getattr(ctl, 'seg', None)
            target = ctl.position_s()
            if seg is None or target is None:
                self._wait_req = 0.0
                self.stop_evt.wait(0.05)
                continue
            # 等片段缓存好：最多干等 3 秒就放慢请求节奏，别把 HTTP 线程
            # 手里的 CPU 全用来空转。
            if seg['file'] != self._seg:
                info = self._grab_seg()
                if info is None:
                    if self._wait_req == 0.0:
                        self._wait_req = time.monotonic()
                    d = time.monotonic() - self._wait_req
                    self.stop_evt.wait(0.05 if d < self.OPEN_TIMEOUT else 0.3)
                    continue
                self._wait_req = 0.0
                if not self._open(info, seg, target):
                    self.stop_evt.wait(0.3)
                    continue
            elif abs(target - self._pos) > self.FOLLOW_S:
                self._seek(target)

            if ctl.paused or ctl.ended:
                self.stop_evt.wait(0.02)
                continue
            step = 1.0 / (self._fps * ctl._speed())
            now = time.monotonic()
            if self._next_t <= 0:
                self._next_t = now
            due = int((now - self._next_t) / step) + 1
            due = max(1, min(6, due))
            ok, frame = False, None
            for _ in range(due):
                ok, frame = self.cap.read()
                if not ok:
                    break
                self._pos += 1.0 / self._fps
            if not ok:
                # 段尾读完：等 ctl.tick 换段（它会改 ctl.seg，下轮重开）
                self._next_t = 0.0
                self.stop_evt.wait(0.05)
                continue
            self._next_t += due * step
            if self._next_t < now - step * 6:
                self._next_t = now
            try:
                jpg = encode_jpg_locked(fit_pad(frame))
            except Exception as e:
                print('回放编码失败: %s: %s' % (type(e).__name__, e),
                      flush=True)
                continue
            if jpg is not None:
                self._pub(jpg)


class PadHub:
    """按当前模式把某一路 tap 接进 /stream.mjpg。

    切换时给流换个代号：正在推的客户端立刻收尾，平板 1 秒后重连
    就拿到新源。切到回放时还得主动请求一次目标段，不然平板要等
    监看台自己先换段才看到画面。
    """

    def __init__(self, ctl):
        self.ctl = ctl
        # 两条"基础"通道：各由一条 tap 持续填充（有人看时才编码）。
        self.live = PadStream()
        self.replay = PadStream()
        # /stream.mjpg 自己的通道：PadHub 在直播时把 live 的新帧转发进来，
        # 回放时让 PadReplayTap 直接往里发。tap 只判断"自己在不在放"，
        # 不负责决定给哪条通道——不然两路 tap 会同时往一个流里写。
        self.pad = PadStream()
        self._pad_is_replay = False
        self._live_seq = -1
        self.gen = 1
        self._mode = None
        self._seg = None
        self._req_seg = None
        self.live_tap = None
        self._grab_n = -1
        self._grab_next = 0.0
        self._relay_next = 0.0

    def stream(self, mode=None):
        if mode == 'live':
            return self.live
        if mode == 'replay':
            return self.replay
        if self._pick() == 'replay':
            self._pad_is_replay = True
        return self.pad

    RELAY_MS = 40.0

    def relay(self):
        """把直播通道的新帧转给 /stream.mjpg（回放时不做）。"""
        if self._pad_is_replay:
            return
        src = self.live
        with src.cv:
            if src.seq == self._live_seq or src.jpg is None:
                return
            # 转发的是引用：下面每次 publish 都会换掉 src.jpg，不会中途变
            jpg = src.jpg
        # 攒够一帧间隔再发：pump 可能 60Hz 跑，逐帧 publish 会把 20fps
        # 的编码帧拆成一个个空等，平板端多轮 wait 才凑出一张。
        now = time.monotonic()
        if now < self._relay_next:
            return
        with src.cv:
            if src.seq == self._live_seq:
                return
            self._live_seq = src.seq
        self._relay_next = max(now, self._relay_next + self.RELAY_MS / 1000.0)
        self.pad.publish(jpg, gen=self.gen)

    def serve(self):
        """pump 每轮调一次：只有真有客户端挂着才推。

        以前只在队列空掉的那支里调 tick/relay，Tk 主线程一忙
        （50ms 的 tick、或连续命令）就整段跳过，直播掉到 0.5fps。
        """
        self.tick()
        if not self.pad.want():
            return
        if not self._pad_is_replay:
            self._grab_live()
        self.relay()

    def _grab_live(self):
        """pump 里补一颗直播帧：等价于 PadLiveTap 的 else 分支。

        PadLiveTap 自己要求在 1/LIVE_FPS 内主动等，pump 一忙就把这个
        等待变成整段丢弃；这里按"没到点就不编"处理，凑满帧率。
        """
        tap = getattr(self, 'live_tap', None)
        if tap is not None and tap.always:
            return              # 浏览器式常开 tap 已经在编，别重复编
        feed = self.ctl.feed
        img = feed.image
        n = feed.image_n
        if img is None or n == self._grab_n:
            return
        now = time.monotonic()
        if now < self._grab_next:
            return
        self._grab_next = max(now, self._grab_next + 1.0 / LIVE_FPS)
        self._grab_n = n
        try:
            jpg = encode_jpg_locked(fit_pad(img))
        except Exception as e:
            print('直播编码失败: %s: %s' % (type(e).__name__, e), flush=True)
            return
        if jpg is not None:
            self.live.publish(jpg)

    def _pick(self):
        return 'replay' if self.ctl.mode == 'replay' else 'live'

    def tick(self):
        """模式或段变了就换代号，让 /stream.mjpg 重建连接。"""
        m = self._pick()
        seg = None
        if m == 'replay':
            s = getattr(self.ctl, 'seg', None)
            seg = s['file'] if s is not None else None
        if m != self._mode:
            self._mode = m
            self._seg = seg
            self.gen += 1
            # 老代号：正在推的 /stream.mjpg 客户端下一轮自己退出。
            # 代号不变，只是 /stream 不重置 latest —— 重连后立刻有画面，
            # 不用等下一帧（暂停回放时根本不会有下一帧）。
            self.pad.switch(self.gen)
            self._live_seq = -1
            self._pad_is_replay = (m == 'replay')
            print('平板画面切到 %s' % ('回放' if m == 'replay'
                                     else '直播'), flush=True)
        else:
            self.relay()
        if self.ctl.mode == 'replay' and seg and seg != self._req_seg:
            # 监看台切段后要主动把新段排进加载队列，否则只等它自己
            # 在 tick 里 request(idle_only=True)，平板会多黑几秒。
            self._req_seg = seg
            s = self.ctl.seg
            if s is not None:
                self.ctl.loader.request(s)

    def close(self):
        self.gen += 1
        self.pad.switch(self.gen)
        self.live.switch(self.gen)
        self.replay.switch(self.gen)


# ---------------- 服务本体 ----------------

class PadServer:
    """在监看台进程里起 HTTP 线程，把状态和画面暴露给平板。

    RefereeServer 是旧名字，保留别名；行为一样，多了画面和遥控。
    """

    PORT_TRIES = 4

    def __init__(self, ctl, port=8610, host='0.0.0.0'):
        self.ctl = ctl
        self.port = port
        self.host = host
        self.srv = None
        self._thread = None
        self.hub = PadHub(ctl)
        self._qb = queue.Queue()
        self._page = 0
        self._lib_sel = 0
        self._msg = ''
        self._msg_t = 0.0
        self._info = ''
        # 录像库的远端列表走 ssh，不能放在 HTTP 请求线程里：缓存放这里，
        # pump 每 5 秒让后台线程刷一次。
        self._remote_sess = []
        self._remote_t = 0.0
        self._seg_t = 0.0
        self._seg_cache = None
        self._remote_busy = False

    # ---- 生命周期 ----

    def start(self):
        handler = _make_handler(self)
        want = self.port
        self.srv = None
        for i in range(self.PORT_TRIES):
            port = want + i
            try:
                # allow_reuse_address=True：重启时旧连接的 TIME_WAIT /
                # FIN_WAIT 不该算「端口被占」。真要撞上别的进程在 LISTEN，
                # bind 依然会 EADDRINUSE，顺延逻辑不受影响。
                self.srv = ThreadingHTTPServer((self.host, port), handler,
                                               bind_and_activate=False)
                self.srv.allow_reuse_address = True
                # 平板断流后 1 秒就重连，默认 backlog=5 一次堆满。
                self.srv.request_queue_size = 64
                self.srv.server_bind()
                self.srv.server_activate()
            except OSError as e:
                self.srv = None
                if i == self.PORT_TRIES - 1:
                    print('裁判数据服务启动失败（端口 %d~%d）: %s'
                          % (want, want + self.PORT_TRIES - 1, e), flush=True)
                    return False
                print('端口 %d 被占用（%s），改试 %d' % (port, e, port + 1),
                      flush=True)
                continue
            if port != want:
                print('裁判数据服务改用端口 %d（%d 被占用）' % (port, want),
                      flush=True)
            self.port = port
            break
        self.srv.daemon_threads = True
        self._thread = threading.Thread(target=self.srv.serve_forever,
                                        daemon=True)
        self._thread.start()
        self._live_tap = PadLiveTap(self.ctl, self.hub.live)
        # 没人看就不编码：PadHub.serve() 在 pump 里按预算补帧，
        # 浏览器调试 /live.mjpg 挂上来时才让 Tap 常开。
        self._live_tap.always = False
        self._live_tap.start()
        self.hub.live_tap = self._live_tap
        self._replay_tap = PadReplayTap(self.ctl, self.hub)
        self._replay_tap.start()
        print('裁判数据服务: http://%s:%d/  （平板 /stream.mjpg）'
              % (self.host, self.port), flush=True)
        return True

    def stop(self):
        try:
            self.hub.close()
        except Exception:
            pass
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    # ---- 与 Tk 主线程的桥 ----

    def exec(self, fn, timeout=2.0):
        """把 fn 交给 Tk 主线程执行并等结果；超时抛 TimeoutError。"""
        fut = Future()
        self._qb.put((fn, fut))
        return fut.result(timeout=timeout)

    def pump(self, budget_ms=8):
        """Tk 主线程每轮调一次：最多干 budget_ms 毫秒的活，不饿死界面。

        队列里可能有回放 seek（要开 VideoCapture，几十到几百 ms），
        一次只起一个就把预算用完的风险留给下一轮，别把窗口卡住。
        """
        t0 = time.monotonic()
        while True:
            left = budget_ms / 1000.0 - (time.monotonic() - t0)
            if left <= 0:
                return
            try:
                fn, fut = self._qb.get_nowait()
            except queue.Empty:
                self.hub.serve()
                self._refresh_remote()
                return
            if fut is not None and fut.set_running_or_notify_cancel() is False:
                continue
            try:
                res = fn()
            except Exception as e:
                if fut is not None:
                    fut.set_exception(e)
            else:
                if fut is not None:
                    fut.set_result(res)

    def _refresh_remote(self):
        """每 5 秒让后台线程刷一次 Jetson 上的场次名。"""
        if self._remote_busy or time.monotonic() - self._remote_t < 5.0:
            return
        self._remote_t = time.monotonic()
        self._remote_busy = True

        def work():
            try:
                self._remote_sess = self.ctl.archiver.list_sessions(20)
            except Exception:
                self._remote_sess = []
            finally:
                self._remote_busy = False

        threading.Thread(target=work, daemon=True).start()

    # ---- 页面与按钮 ----

    def page(self):
        p = PAGES[self._page % len(PAGES)]
        ctl = self.ctl
        labels = list(p[1])
        hl = ['0'] * 4
        if p[0] == 'LIVE':
            hl[0] = '1'
        elif p[0] == 'PLAY':
            # 暂停时按下去是"播"，播放中按下去是"停"
            labels[0] = 'RESUME' if (ctl.paused or ctl.ended) else 'PAUSE'
            labels[1] = ('%gX' % ctl._speed())
            hl[0] = '1'
        elif p[0] == 'SEEK':
            hl[2] = hl[3] = '1'
        elif p[0] == 'MARK':
            n = len(ctl.marks())
            hl[1] = '1' if n else '0'
        elif p[0] == 'LIB':
            sess = self.lib_sessions()
            if sess:
                # 小字显示当前选中场次的时长/大小/标记数；场次名太长
                # 放不进这一行，只在翻页提示里给尾段。场次名为空就说明
                # 还没刷到，异步刷一遍。
                if not self._remote_busy:
                    self._refresh_remote()
                s = sess[self._lib_sel % len(sess)]
                self._info = '%02d:%02d %dMB %dM' % (
                    int(s.get('seconds') or 0) // 60,
                    int(s.get('seconds') or 0) % 60,
                    (s.get('bytes') or 0) // (1024 * 1024),
                    int(s.get('marks') or 0))
            else:
                self._info = 'NO SESSION'
            hl[2] = hl[3] = '1'
        return p[0], labels, ''.join(hl)

    def flip(self, d):
        if d:
            self._page = (self._page + (1 if d > 0 else -1)) % len(PAGES)
        return self._page

    def say(self, s):
        self._msg = _ascii(s, 40)
        self._msg_t = time.monotonic()

    def press(self, pg, slot, fresh=None):
        """按页面按钮。pg 与当前页不符 = 平板上画的是旧按钮，不执行。

        fresh 是这一次请求里带上的最新状态（平板 status 刚到），用来
        判断按钮此刻是否还有意义——比如列表已空还按 DEL 就不该执行。
        """
        name, labels, hl = self.page()
        if pg is not None and pg != self._page:
            return {'ok': 0, 'err': 'stale', 'pg': self._page}
        if not (0 <= slot < 4):
            return {'ok': 0, 'err': 'slot'}
        # 槽位标签会随状态变（play/pause、1x/0.5x），所以按"基线标签"
        # 分发，不能用平板上看到的那个字符串。
        label = PAGES[self._page % len(PAGES)][1][slot]
        if not label:
            return {'ok': 0, 'err': 'empty'}
        try:
            ok = self._do(name, label, fresh or {})
        except Exception as e:
            print('平板按钮 %s/%s 执行失败: %s: %s'
                  % (name, label, type(e).__name__, e), flush=True)
            return {'ok': 0, 'err': 'error'}
        return {'ok': 1 if ok is not False else 0, 'pg': self._page,
                'label': LABELS.get(label, label)}

    def _do(self, page, label, fresh):
        ctl = self.ctl
        if label == 'mark':
            m = self.exec(lambda: ctl.add_mark(note='PAD'))
            self.say('MARK %s' % (m.get('clock', '')))
            return True
        if label == 'del':
            n = len(ctl.marks())
            if not n:
                self.say('NO MARK')
                return False
            r = self.exec(lambda: ctl.remove_mark(n - 1))
            self.say('DEL %s' % ('OK' if r else 'FAIL'))
            return bool(r)
        if label == 'list':
            ms = ctl.marks()
            tail = ' | '.join(
                '#%d %s %s' % (i, _ascii(m.get('clock', ''), 8),
                               _ascii(m.get('note', ''), 10))
                for i, m in enumerate(ms[-3:])) or 'NO MARK'
            self.say(tail)
            return True
        if label == 'summary':
            # 平板右侧常驻标记列表，这个按钮只是把提示刷回一行概括
            self._msg = ''
            return True
        if label == 'note':
            idx = int(fresh.get('index', -1))
            tag = _ascii(fresh.get('tag', ''), 16)
            if idx < 0 or not tag:
                n = len(ctl.marks())
                if not n:
                    self.say('NO MARK')
                    return False
                idx = n - 1
                tag = 'PAD'
            r = self.exec(lambda: ctl.note_mark(idx, tag))
            self.say('NOTE %s %s' % (idx, 'OK' if r else 'FAIL'))
            return bool(r)
        if label == 'replay':
            segs = ctl.segments()
            if not segs:
                self.say('NO SEGMENT')
                return False
            self.exec(ctl.to_replay)
            self._page = 1
            return True
        if label == 'live':
            self.exec(ctl.to_live)
            self._page = 0
            return True
        if label == 'toggle':
            self.exec(ctl.toggle_play)
            return True
        if label == 'speed':
            # speed_idx 初值来自 args.speed（浮点 1.0），先取整再循环
            self.exec(lambda: ctl.set_speed((int(ctl.speed_idx) + 1) % 3))
            return True
        if label.startswith('step'):
            d = -1 if label.endswith('-1') else 1
            self.exec(lambda: ctl.step(d))
            return True
        if label.startswith('seek'):
            if label in ('seek-5', 'seek+5'):
                target = ctl.position_s() + (-5 if label.endswith('-5')
                                             else 5)
            else:
                # mark- / mark+：按时间顺序找上一条/下一条标记
                fwd = label.endswith('>')
                cur = ctl.position_s()
                cand = [m['t_s'] for m in ctl.marks()
                        if (m['t_s'] > cur + 0.05 if fwd
                            else m['t_s'] < cur - 0.05)]
                if not cand:
                    self.say('NO MARK ' + label)
                    return False
                target = min(cand) if fwd else max(cand)
            target = max(0.0, target)
            self.exec(lambda: ctl.seek(target))
            return True
        if label == 'lib':
            self._page = 4
            self._lib_sel = 0
            return True
        if label in ('prev', 'next'):
            sess = self.lib_sessions()
            if sess:
                self._lib_sel = (self._lib_sel + (-1 if label == 'prev'
                                                  else 1)) % len(sess)
                s = sess[self._lib_sel]
                sec = int(s.get('seconds') or 0)
                self.say('%s %02d:%02d %dMB' % (
                    _ascii(s.get('name', ''), 12), sec // 60, sec % 60,
                    (s.get('bytes') or 0) // (1024 * 1024)))
            return True
        if label == 'open':
            sess = self.lib_sessions()
            if not sess:
                self.say('NO SESSION')
                return False
            s = sess[self._lib_sel % len(sess)]
            if s.get('src') != 'local':
                self.say('ONLY LOCAL OPEN')
                return False
            d = s['dir']
            self.exec(lambda: ctl.open_session(d))
            self._page = 1
            return True
        if label == 'archive':
            sess = self.lib_sessions()
            if not sess:
                self.say('NO SESSION')
                return False
            name = sess[self._lib_sel % len(sess)]['name']
            if ctl.archive_busy:
                self.say('ARC BUSY')
                return False

            def do_arc():
                info = ctl.archiver.info(name)
                if info is None:
                    self.say('ARC NO SESSION')
                    return False
                return ctl.start_archive(ctl.archive_dir,
                                         remove_remote=False, sess=name,
                                         total=info.get('bytes', 0))

            return self.exec(do_arc)
        return False

    # ---- 录像库 ----

    def lib_sessions(self):
        """本地归档场次（可打开/归档）+ 远端场次（只能归档）。"""
        out = []
        try:
            for s in self.ctl.library.list_sessions():
                out.append(s)
        except Exception:
            pass
        have = {s['name'] for s in out}
        for name in self._remote_sess:
            if name not in have:
                out.append({'name': name, 'dir': None, 'seconds': 0.0,
                            'bytes': 0, 'marks': 0, 'src': 'remote'})
        for s in out:
            s['src'] = 'local' if s.get('dir') else 'remote'
        out.sort(key=lambda s: s['name'], reverse=True)
        return out

    def lib_ctl(self, op):
        ctl = self.ctl
        if op in ('prev', 'next'):
            return self._do('LIB', op, {})
        if op == 'open':
            return self._do('LIB', 'open', {})
        if op == 'archive':
            return self._do('LIB', 'archive', {})
        return False

    # ---- 状态 ----

    def status(self):
        ctl = self.ctl
        name, labels, hl = self.page()
        try:
            tc = ctl.timecode()
        except Exception:
            tc = ''
        mode = 'replay' if ctl.mode == 'replay' else 'live'
        info = ''
        if mode == 'live':
            info = '%.1fFPS' % ctl.feed.fps() if ctl.feed.alive() else 'NO SIGNAL'
        else:
            info = '%gX %s' % (ctl._speed(),
                               'PAUSE' if (ctl.paused or ctl.ended) else 'PLAY')
            if ctl.archive_busy:
                info = 'ARC %d%%' % (
                    int(100.0 * ctl.archive_done / ctl.archive_total)
                    if ctl.archive_total else 0)
        if name == 'LIB':
            info = self._info
        if self._msg and time.monotonic() - self._msg_t < 4.0:
            info = self._msg
        st = {
            'ok': 1,
            'timecode': _ascii(tc, 40),
            'sess_label': _ascii(ctl._session_label(), 24),
            'mode': mode,
            'mark_count': len(ctl.marks()),
            'page': name,
            'pg': self._page,
            'pn': len(PAGES),
            'btn0': LABELS.get(labels[0], labels[0]),
            'btn1': LABELS.get(labels[1], labels[1]),
            'btn2': LABELS.get(labels[2], labels[2]),
            'btn3': LABELS.get(labels[3], labels[3]),
            'hl': hl,
            'info': _ascii(info, 30),
            'pos': round(ctl.position_s(), 1),
            'ts': round(time.time(), 3),
        }
        msgs = getattr(ctl, 'msg', '')
        if msgs and time.monotonic() - getattr(ctl, 'msg_t', 0.0) < 4.0:
            st['msg'] = _ascii(msgs, 40)
        return st

    def hud(self):
        """面板化 UI 要的数据：时间轴、标记、场次库、会话窗口。

        跟 status() 分开是怕把它撑爆——它只给 40 字节级的短字段。
        这些全是**只读**属性（与 UI 读同一份数据），HTTP 线程直接读，
        不进 Tk 主线程队列，免得 1 秒一次的轮询把界面卡住。
        """
        ctl = self.ctl
        st = self.status()
        out = {
            'ok': st.get('ok', 0),
            'mode': st.get('mode', 'live'),
            'timecode': st.get('timecode', ''),
            'sess_label': st.get('sess_label', ''),
            'info': st.get('info', ''),
            'page': st.get('page', ''),
            'pg': st.get('pg', 0),
            'pn': st.get('pn', 0),
            'hl': st.get('hl', '0000'),
            'mark_count': st.get('mark_count', 0),
            'ts': st.get('ts', 0),
        }
        for i in range(4):
            out['btn%d' % i] = st.get('btn%d' % i, '')
        out['msg'] = st.get('msg', '')

        pos, dur, t0, t1 = 0.0, 0.0, 0.0, 0.0
        segs = []
        try:
            pos = float(ctl.position_s())
            dur = float(ctl.timeline_end())
            # segments_window 每轮扫分段目录并归并，放 HTTP 线程里就是一颗
            # 定时炸弹：扫目录时 Tk 主线程正常，唯独这一路 400ms 轮询会
            # 卡住——平板状态就停在第一份上。只在 pump 里 2s 刷一次。
            if time.monotonic() - self._seg_t > 2.0:
                self._seg_t = time.monotonic()
                try:
                    w, w0 = ctl.segments_window(600.0)
                    self._seg_cache = (
                        float(w0), max(float(w0) + 1.0, dur),
                        [(round(max(float(x['start_s']), float(w0)), 1),
                          round(min(float(x['end_s']),
                                    max(float(w0) + 1.0, dur)), 1))
                         for x in w[-16:]])
                except Exception:
                    self._seg_cache = None
            if self._seg_cache is not None:
                t0, t1, segs = self._seg_cache
            else:
                t0, t1 = 0.0, max(1.0, dur)
        except Exception:
            pass
        out['pos'] = round(pos, 1)
        out['dur'] = round(dur, 1)
        out['t0'] = round(t0, 1)
        out['t1'] = round(t1, 1)
        out['segs'] = segs

        try:
            out['marks'] = _marks(ctl)['marks'][-20:]
        except Exception:
            out['marks'] = []

        try:
            sess = self.lib_sessions()
            out['lib'] = [{'i': i,
                           'name': _ascii(s.get('name', ''), 24),
                           'sec': int(round(s.get('seconds') or 0)),
                           'mb': int((s.get('bytes') or 0) // (1024 * 1024)),
                           'marks': int(s.get('marks') or 0),
                           'src': s.get('src') or 'local'}
                          for i, s in enumerate(sess[:12])]
            out['lib_sel'] = self._lib_sel
            info = self._info
            if out['page'] == 'LIB' and info:
                # LIB 页副行原样展示（时长/大小/标记数），面板上没地方另放
                out['info'] = info
        except Exception:
            out['lib'] = []
            out['lib_sel'] = 0
        return out

    def select_mark(self, i):
        """点标记列表里的一条：状态栏给个提示（真正的跳转由 seek 做）。"""
        try:
            ms = list(self.ctl.marks())
            i = int(i)
            if not (0 <= i < len(ms)):
                return {'ok': 0, 'err': 'index'}
            m = ms[i]
            self.say('#%d %s %s' % (i, _ascii(m.get('clock', ''), 10),
                                    _ascii(m.get('note', ''), 12)))
            return {'ok': 1}
        except Exception:
            return {'ok': 0, 'err': 'bad'}


# 旧名字：studio_app.py 早先就 import 这个
RefereeServer = PadServer


# ---------------- HTTP 层 ----------------

def _make_handler(srv):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        # ---- 分发 ----

        def do_GET(self):
            p = self.path.split('?')[0]
            if p == '/api/status':
                self._json(srv.status())
            elif p == '/api/hud':
                self._json(srv.hud())
            elif p == '/api/marks':
                self._json(_marks(srv.ctl))
            elif p == '/api/lib/sessions':
                self._json(_lib_json(srv))
            elif p == '/snapshot.jpg':
                self._snapshot(srv)
            elif p in ('/', '/index.html'):
                self._html(_page_html())
            elif p in ('/stream.mjpg', '/live.mjpg', '/replay.mjpg'):
                mode = {'/live.mjpg': 'live',
                        '/replay.mjpg': 'replay'}.get(p)
                self._mjpeg(srv, mode)
            else:
                self.send_error(404)

        def do_POST(self):
            p = self.path.split('?')[0]
            req = self._body()
            if req is None:
                return
            if p == '/api/mark':
                note = _ascii(req.get('note') or '', 32) or None
                pre = max(0.0, min(30.0, float(req.get('pre_s') or 0)))
                r = srv.exec(lambda: srv.ctl.add_mark(pre_s=pre, note=note))
                srv.say('MARK %s' % r.get('clock', ''))
                self._json({'ok': 1, 'mark': r})
            elif p == '/api/unmark':
                idx = int(req.get('index', -1))
                n = req.get('n')
                cur = len(srv.ctl.marks())
                if n is not None and int(n) != cur:
                    self._json({'ok': 0, 'err': 'stale', 'n': cur})
                    return
                ok = srv.exec(lambda: srv.ctl.remove_mark(idx))
                self._json({'ok': 1 if ok else 0})
            elif p == '/api/note':
                r = srv._do('MARK', 'note', req)
                self._json({'ok': 1 if r else 0})
            elif p == '/api/ui/page':
                pg = srv.flip(int(req.get('dir', 1)))
                self._json({'ok': 1, 'pg': pg})
            elif p == '/api/ui/press':
                self._json(srv.press(req.get('pg'), int(req.get('slot', -1)),
                                     req))
            elif p == '/api/mark/select':
                self._json(srv.select_mark(req.get('index', -1)))
            elif p == '/api/replay/open':
                sess = req.get('session') or ''
                found = [s for s in srv.lib_sessions() if s['name'] == sess]
                if not found or not found[0].get('dir'):
                    self._json({'ok': 0, 'err': 'no_session'})
                else:
                    d = found[0]['dir']
                    name = srv.exec(lambda: srv.ctl.open_session(d),
                                    timeout=6.0)
                    self._json({'ok': 1 if name else 0, 'session': name})
            elif p == '/api/replay/ctl':
                self._json(self._replay_ctl(srv, req))
            elif p == '/api/lib/ctl':
                ok = srv.lib_ctl(req.get('op') or '')
                self._json({'ok': 1 if ok else 0})
            elif p == '/api/live':
                # to_live() 无返回值，执行成功即为 ok
                srv.exec(srv.ctl.to_live)
                self._json({'ok': 1})
            else:
                self.send_error(404)

        def _replay_ctl(self, srv, req):
            ctl = srv.ctl
            op = req.get('op') or ''
            if op == 'play':
                # 播完再从这条路径按下：光清 ended 没用——cap 已经停在
                # 段尾，tick 第一帧就读失败又立刻判成播完。跟 toggle_play
                # 一样跳回段头。
                def _play():
                    if ctl.ended and ctl.seg is not None:
                        ctl.seek(ctl.seg['start_s'])
                    ctl.paused = False
                    ctl.ended = False
                fn = _play
            elif op == 'pause':
                fn = lambda: setattr(ctl, 'paused', True)
            elif op == 'toggle':
                fn = ctl.toggle_play
            elif op == 'step':
                n = int(req.get('n', 1))
                fn = lambda: ctl.step(1 if n > 0 else -1)
            elif op == 'speed':
                idx = int(req.get('idx', (int(ctl.speed_idx) + 1) % 3))
                fn = lambda: ctl.set_speed(max(0, min(2, idx)))
            elif op == 'seek_rel':
                dt = float(req.get('dt', 0))
                fn = lambda: ctl.seek(max(0.0, ctl.position_s() + dt))
            elif op == 'seek_pos':
                t = float(req.get('t', 0))
                fn = lambda: ctl.seek(t)
            elif op in ('next_mark', 'prev_mark'):
                fwd = op == 'next_mark'
                cur = ctl.position_s()
                cand = [m['t_s'] for m in ctl.marks()
                        if (m['t_s'] > cur + 0.05 if fwd
                            else m['t_s'] < cur - 0.05)]
                if not cand:
                    return {'ok': 0, 'err': 'no_mark'}
                t = min(cand) if fwd else max(cand)
                fn = lambda: ctl.seek(t)
            else:
                return {'ok': 0, 'err': 'op'}
            srv.exec(fn)
            return {'ok': 1, 'pos': round(ctl.position_s(), 2),
                    'paused': bool(ctl.paused or ctl.ended),
                    'speed': ctl._speed()}

        # ---- 静态 ----

        def _snapshot(self, srv):
            st = srv.hub.stream()
            gen = st.enter()
            # 有最新帧就直接给，不等下一张：暂停着的回放不会有下一帧。
            jpg = None
            with st.cv:
                if st.jpg is not None:
                    gen, jpg = st.gen, st.jpg
            if jpg is None and st.want() > 0:
                # 已经有客户端在拉 /stream.mjpg 时才有下一帧可等；
                # 没人拉流的话编码器是关着的，干等 3 秒必超时。
                jpg, _ = st.wait(-1, gen, timeout=3.0)
            st.leave()
            if jpg is None:
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self._raw(jpg, 'image/jpeg')

        def _mjpeg(self, srv, mode):
            st = srv.hub.stream(mode)
            if mode == 'live':
                # 浏览器调试挂 /live.mjpg：让 Tap 常开，编码交给它
                srv.hub.live_tap.always = True
            gen = st.enter()
            try:
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                seq = -1
                last_t = 0.0
                sent = None
                while True:
                    jpg, seq2 = st.wait(seq, gen, timeout=2.0)
                    # 换源：这一轮的流到此为止。平板 stream_task 读到
                    # 空行/断开后会 1 秒重连，自动跟上新画面。
                    if gen != st.gen:
                        break
                    if jpg is not None and seq2 != seq:
                        seq = seq2
                        sent = jpg
                        last_t = time.monotonic()
                    elif time.monotonic() - last_t > 2.0:
                        # 暂停时不出新帧，重发末帧防对端 10 秒超时断连。
                        # 一开始就没帧（直播 NO SIGNAL）时连末帧都没有，
                        # 回一张空占位帧——固件照样能叠 OSD，至少每 2 秒
                        # 收一次数据，不会被自己的 recv 超时踢掉。
                        if sent is None:
                            sent = blank_jpg()
                        jpg = sent
                        if jpg is None:
                            continue
                        last_t = time.monotonic()
                    else:
                        continue
                    self.wfile.write(b'--frame\r\n'
                                     b'Content-Type: image/jpeg\r\n'
                                     b'Content-Length: '
                                     + str(len(jpg)).encode() + b'\r\n\r\n')
                    self.wfile.write(jpg)
                    self.wfile.write(b'\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                st.leave()
                if mode == 'live' and not srv.hub.live.want():
                    srv.hub.live_tap.always = False

        # ---- 工具 ----

        def _body(self):
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b'{}'
            try:
                d = json.loads(raw or b'{}')
            except ValueError:
                d = {}
            return d if isinstance(d, dict) else {}

        def _ok(self, srv, fn):
            try:
                fn()
                self._json({'ok': 1})
            except Exception as e:
                self._json({'ok': 0, 'err': str(e)})

        def _raw(self, data, ctype):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj):
            data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self._raw(data, 'application/json; charset=utf-8')

        def _html(self, s):
            self._raw(s.encode('utf-8'), 'text/html; charset=utf-8')

        def log_message(self, *a):
            pass

    return Handler


def _marks(ctl):
    try:
        raw = list(ctl.marks())
    except Exception:
        raw = []
    out = []
    for i, m in enumerate(raw):
        t_s = float(m.get('t_s', 0.0))
        # 平板右侧面板只有 136px 宽，用 m:ss 而不是 hh:mm:ss，否则会被截掉
        out.append({'i': i,
                    't_s': round(t_s, 2),
                    't': '%02d:%04.1f' % (int(t_s) // 60, t_s % 60),
                    'tag': _ascii(m.get('note', ''), 16)})
    return {'sel': -1, 'n': len(out), 'marks': out}


def _lib_json(srv):
    sess = srv.lib_sessions()
    out = []
    for i, s in enumerate(sess):
        out.append({
            'i': i,
            'name': _ascii(s.get('name', ''), 40),
            'label': _ascii(s.get('name', ''), 20),
            'sec': int(round(s.get('seconds') or 0)),
            'mb': int((s.get('bytes') or 0) // (1024 * 1024)),
            'marks': int(s.get('marks') or 0),
            'src': s.get('src') or 'local',
        })
    return {'sel': srv._lib_sel, 'n': len(out), 'sessions': out}


_PAGE_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>裁判监看台</title>
<style>
  body{margin:0;background:#0a0b0e;color:#eef1f6;
       font-family:system-ui,-apple-system,"Noto Sans CJK SC",sans-serif}
  #v{width:100%;height:calc(100vh - 64px);object-fit:contain;background:#000}
  #hud{position:fixed;top:0;left:0;right:0;padding:10px 14px;
       background:linear-gradient(180deg,rgba(10,11,14,.92),transparent);
       font:13px/1.6 ui-monospace,monospace;display:flex;gap:16px;flex-wrap:wrap}
  #bar{position:fixed;bottom:0;left:0;right:0;padding:12px;display:flex;
       gap:10px;align-items:center;background:#14161c}
  button{font:inherit;font-size:15px;padding:12px 26px;border-radius:10px;
         border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.08);
         color:#eef1f6;cursor:pointer}
  button:active{transform:scale(.96)}
  .k{color:#8b93a3}
</style></head><body>
<img id="v" alt="" src="/stream.mjpg">
<div id="hud"><span class="k">时间码</span><b id="tc">—</b>
  <span class="k">场次</span><b id="sess">—</b>
  <span class="k">模式</span><b id="mode">—</b>
  <span class="k">标记</span><b id="nm">0</b>
  <span class="k">页</span><b id="pg">—</b></div>
<div id="bar"><button id="mk">打标记</button>
  <button id="prev">上页</button><button id="next">下页</button>
  <span id="msg" class="k"></span></div>
<script>
const $=id=>document.getElementById(id);
async function poll(){
  try{
    const d=await (await fetch('/api/status',{cache:'no-store'})).json();
    $('tc').textContent=d.timecode||'—';
    $('sess').textContent=d.sess_label||'—';
    $('mode').textContent=d.mode==='live'?'直播':'回放';
    $('nm').textContent=d.mark_count;
    $('pg').textContent=(d.page||'')+' '+(d.pg+1)+'/'+d.pn;
    $('msg').textContent=d.msg||d.info||'';
  }catch(e){ $('tc').textContent='断开'; }
}
async function post(u,b){
  await fetch(u,{method:'POST',body:JSON.stringify(b||{})});
}
$('mk').onclick=()=>post('/api/mark',{note:'WEB'});
$('prev').onclick=()=>post('/api/ui/page',{dir:-1});
$('next').onclick=()=>post('/api/ui/page',{dir:1});
setInterval(poll,400); poll();
</script></body></html>
"""


def _page_html():
    return _PAGE_HTML
