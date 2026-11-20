#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛事监看台：数据 / 服务层（与 UI 完全解耦）

所有类都不 import 任何 GUI 库，只做「连接 RK3588、取流、拉片段、远端控制」。
未来局域网移动端遥控器可以直接复用本模块，UI 只负责展示与交互。

分层：
  LiveFeed    直播解码线程：TCP 裸 HEVC → NVDEC/PyAV → 最新帧（BGR ndarray）
  Archive     远端录制目录轮询线程：拿到已封存分段列表 meta
  ClipLoader  拉取远端分段 → scp → 本地 remux 缓存，供回放定位
  RemoteCtrl  通过 ssh 启动/停止 RK3588 录制管线
"""
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections import deque

import numpy as np
import cv2

from dgx_viewer_rk import LiveDecoder, connect
from hk_replay_fetch import remux_mkv


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def fmt_time(s):
    """秒 → mm:ss 或 h:mm:ss"""
    s = max(0, int(s))
    if s >= 3600:
        return '%d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60)
    return '%02d:%02d' % (s // 60, s % 60)


def default_record_dir():
    """录像归档的默认根目录：本机 /home/ifit/xjh/replay/archive。

    launch 时若 archive.json 里记过别的目录，以那份为准（见 Controller）。
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'replay', 'archive_rk')


def read_session_dir(path):
    """把任意位置的场次目录读成回放分段列表；不是场次目录返回 None。

    「打开录像」用的入口：目录里只要有 meta.jsonl 且至少一段能读到文件，
    就当作一场录像。字段口径与 Archive._refresh / Archiver.load_local
    完全一致（dir/start_s/end_s/frames/bytes），下游不用区分来源。
    返回 (场次名, 分段列表, 本场起点 epoch 毫秒或 None)。
    """
    path = os.path.abspath(os.path.expanduser(path or ''))
    meta = os.path.join(path, 'meta.jsonl')
    if not os.path.isfile(meta):
        return None
    segs = []
    base = None
    try:
        with open(meta, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            rec = json.loads(line)
            rec['dir'] = path
            rec['start_s'] = rec['start_ms'] / 1000.0
            rec['end_s'] = rec['end_ms'] / 1000.0
        except (ValueError, KeyError):
            continue
        p = os.path.join(path, rec['file'])
        try:
            rec['bytes'] = os.path.getsize(p)
        except OSError:
            continue            # 文件不在就别列，免得点了视频也放不出来
        b = rec.get('base_ms')
        if b is not None:
            start = b - rec['start_ms']
            if base is None or start < base:
                base = start
        segs.append(rec)
    segs.sort(key=lambda r: r['start_s'])
    if not segs:
        return None
    return os.path.basename(path.rstrip('/')), segs, base


def probe_fps(path):
    """数一段 .h265 的实际帧数，除以时长得真实帧率；失败返回 None。

    元数据缺帧数时用它兜底。只做 demux 不做解码，40 MB 的段约 0.1 秒。
    """
    try:
        import av
    except ImportError:
        return None
    n = 0
    try:
        with av.open(path, format='hevc') as c:
            st = c.streams.video[0]
            for pkt in c.demux(st):
                if pkt.size == 0 and pkt.dts is None:
                    continue
                n += 1
    except (OSError, ValueError, IndexError):
        return None
    if n < 2:
        return None
    return n


def list_session_dirs(root):
    """列出 root 下所有像样的场次目录（含 meta.jsonl），新的在前。

    同时认识两种目录名：录制端的 stage1_YYYYmmdd_HHMMSS，以及用手动
    拷过来的普通目录——只要里面有 meta.jsonl 就算，不靠名字前缀猜。
    """
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith('.'):
            continue
        d = os.path.join(root, n)
        if os.path.isfile(os.path.join(d, 'meta.jsonl')):
            out.append(d)
    out.sort(reverse=True)
    return out


# ---------------------------------------------------------------------------
# 直播
# ---------------------------------------------------------------------------

class LiveFeed(threading.Thread):
    """常开直播解码：自动连接 / 断线重连 / 只保留最新帧

    对外属性（线程安全读）：
      image     最新一帧 BGR ndarray（None 表示还没出帧）
      image_t   最新帧的单调时钟时间戳
      fps       最近 2 秒的实时帧率
      alive     连接是否存活
      status    人类可读状态串
      decoder   'nvdec' / 'pyav'
    """

    def __init__(self, host, port, width=1280, want=True):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.width = width
        self._want = want
        self._fps = 59.2

        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.q = queue.Queue(4)
        self._ts = deque(maxlen=128)

        self.image = None
        self.image_t = 0.0
        # 该帧的服务端采集时刻（录制侧秒数）。需要 RK3588 发 SLIVE1 头 +
        # 绝对锚点；没有时为 None，UI 退回按经验值估算。
        self.image_pts = None
        self.image_pts_t = 0.0     # image_pts 对应的本机单调时刻（解码时打点）
        # 服务端录制轴相对本机时钟的偏移（服务端单调钟 - 本机单调钟）。
        # 由 SLIVE1 头附带的服务端单调时刻与收到它的本机时刻配对得出，
        # 直接把 PTS 变成"这帧是此刻拍的吗"，不必按字节码率外推。
        # 重连补偿：上一连接最后一帧位置 / 本连接首帧位置 / 补偿量。
        # 服务端重启后录制轴从 0 重来，不加补偿时间码会跳回整场开头。
        self._last_good_pts = None
        self._seg_first_pos = None
        self._seg_shift = 0.0
        self._seen_seg = -1        # 已处理的连接代号，-1 = 尚未连上
        # 连接起算的"解码帧数 vs PTS 推算帧数"；差值持续拉大 = 本机解码
        # 供帧慢于直播，帧会迟到，延迟会被人为恶化
        self.dec_frames = 0
        self.pts_frames = 0
        self.anchor_wall = None    # 此刻对应的录制轴秒数
        self.anchor_t = 0.0        # 读到锚点时的本机单调时刻
        self.push_t = 0.0          # 最新帧 arrive 时刻（取走后归 0）
        # 画面"传输+解码"延迟样本：每帧交付时刻 - 该帧录制轴时刻。
        # 注意它不含 RK3588 直播分发队列里还没发出来的那部分，所以只当
        # 下限用；真正的总延迟由控制器用画面内容自动对时（见 lat_probe）。
        self._lat_win = deque(maxlen=90)
        self.transport_lat = None
        self.image_n = 0           # 累计取走的帧数（新连接从 0 起）
        self.dec = None
        self.sock = None
        self.status = 'idle'
        self._next_try = 0.0
        self._fast_until = 0.0

    # ---- 外部接口 ----
    def set_want(self, want):
        """True=想要直播（连上），False=主动断开（不重连）"""
        with self.lock:
            self._want = want
        if not want:
            self._teardown()
            self.status = 'off'
        else:
            self._next_try = 0.0

    @property
    def decoder(self):
        return self.dec.decoder if self.dec is not None else '-'

    # 超过这么久没来新帧就算断流：此时画面停在最后一帧上，
    # 时间码也一并停住，不能再按墙钟往前走（否则一恢复就跳回去）。
    STALE_S = 1.5

    def alive(self):
        return (self.image is not None
                and time.monotonic() - self.image_t < self.STALE_S)

    def stale_for(self):
        """画面多久没更新了（秒）；还没出过帧返回 None"""
        if self.image is None:
            return None
        return max(0.0, time.monotonic() - self.image_t)

    def live_pos(self):
        """屏幕上这帧在录制轴上已是第几秒；对不上返回 None。

        解码器用自己的连接内字节增量算出位置（不依赖任何绝对时间钟，
        所以 RK3588 时间不准也照样准）。这里只处理**换连接**：服务端
        重启或重连后录制轴会从 0 重新计，新旧两条轴要接起来。
        """
        dec = self.dec
        if dec is None:
            return None
        # 本连接还没在 _drain 里确认过：先别用，等它把首帧字节起点记好。
        # 用上一连接的 old 状态会让换连接后的头几帧算出整场长度的偏差。
        if dec.last_src_pts is None or self._seen_seg != dec.reconnect:
            return None
        pos = dec.live_pos
        if pos is None:
            return None
        if self._seg_first_pos is None:
            self._seg_first_pos = pos
            last = self._last_good_pts
            # 新连接首帧比旧轴末值早 → 服务端重开了录制轴，补上续接
            self._seg_shift = (last - pos) if last is not None and last > pos \
                else 0.0
        return max(0.0, pos + self._seg_shift)

    def fps(self):
        now = time.perf_counter()
        while self._ts and now - self._ts[0] > 2.0:
            self._ts.popleft()
        if len(self._ts) < 2:
            return 0.0
        span = self._ts[-1] - self._ts[0]
        return (len(self._ts) - 1) / span if span > 0 else 0.0

    def stop(self):
        self.stop_evt.set()
        self._teardown()

    # ---- 内部 ----
    def _open(self):
        try:
            sock = connect(self.host, self.port)
        except OSError as e:
            delay = 1.5 if time.monotonic() < self._fast_until else 5.0
            self.status = '连接失败 (%s)，%gs 后重试' % (e, delay)
            self._next_try = time.monotonic() + delay
            return False
        self.sock = sock
        self.q = queue.Queue(4)
        self._ts.clear()
        self.image_pts = None
        self.image_pts_t = 0.0
        # 新连接的头信息是新的：上一连接的末值 _last_good_pts 要留着做重连补偿
        self._seg_first_pos = None
        self._seg_shift = 0.0
        self._lat_win.clear()
        self.transport_lat = None
        self.image_n = 0
        self.dec_frames = 0
        self.pts_frames = 0
        evt = threading.Event()
        self._dec_evt = evt
        self.dec = LiveDecoder(sock, self.q, evt, self.width)
        self.dec.start()
        self.status = '已连接'
        return True

    def _teardown(self):
        evt = getattr(self, '_dec_evt', None)
        if evt is not None:
            evt.set()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        dec, self.dec = self.dec, None
        if dec is not None:
            dec.join(timeout=1.5)
        with self.lock:
            self.image = None
            self.image_t = 0.0
            # 断流时 PTS 也一起清掉：留着会让界面拿旧值算出跳变
            self.image_pts = None
            self.image_pts_t = 0.0
        self._ts.clear()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def set_anchor(self, wall_s):
        """记录"此刻对应的录制轴秒数"（由控制器用 base_ms 换算）

        与服务端 PTS 同轴，因此可直接算端到端延迟：image_pts - anchor。
        """
        with self.lock:
            self.anchor_wall = wall_s
            self.anchor_t = time.monotonic()

    def _drain(self):
        # 解码器 60fps 不停出帧，队列基本取不空：每轮只处理到"取走的最新
        # 那帧"就够了，否则这个循环永不返回，主循环的定时全被吃掉。
        for _ in range(self.q.qsize()):
            try:
                img = self.q.get_nowait()
            except queue.Empty:
                break
            now = time.monotonic()
            with self.lock:
                self.image = img
                self.image_n += 1
                self.image_t = now
                self.push_t = 0.0
                dec = self.dec
            pts = None
            off = None
            if dec is not None and self.dec is dec:
                if self._seen_seg != dec.reconnect:
                    self._seen_seg = dec.reconnect
                    self._seg_first_pos = None
                    self._seg_shift = 0.0
                pts = getattr(dec, 'last_pts_s', None)
                off = getattr(dec, 'clock_off', None)
            self.image_pts = pts
            self.image_pts_t = now if pts is not None else 0.0
            if pts is not None:
                self._last_good_pts = pts
            # 帧交付时刻（本机时钟）- PTS 换算到本机时钟 = 传输+解码延迟
            if pts is not None and off is not None:
                self._lat_win.append(dec.last_pts_t - (pts + off))
                self.transport_lat = sorted(self._lat_win)[
                    len(self._lat_win) // 2]
            if pts is not None:
                if dec.first_pts_s is None:
                    dec.first_pts_s = pts
                self.dec_frames += 1
                self.pts_frames = max(
                    0, int(round((pts - dec.first_pts_s) * self._fps)))
            self._ts.append(time.perf_counter())

    def run(self):
        while not self.stop_evt.is_set():
            with self.lock:
                want = self._want
            if not want:
                self.stop_evt.wait(0.2)
                continue
            if self.dec is not None and self.dec.is_alive():
                self._drain()
                # 帧到达后若一直没人取走，就记下"最新帧卡了多久"——
                # 本机解码器出帧速度会随负载波动，帧迟到才到，画面上看
                # 是延迟变大，但帧本身的时间戳没错；测延迟要扣掉这段。
                if (self.image is not None and self.image_t
                        and not self.push_t):
                    self.push_t = time.monotonic()
                self.stop_evt.wait(0.005)
                continue
            if self.dec is not None:
                # 已死：清理后重连
                self._teardown()
                self._next_try = time.monotonic() + 5.0
                self.status = '断线，5s 后重连'
            if time.monotonic() >= self._next_try:
                self._open()
            self.stop_evt.wait(0.05)


# ---------------------------------------------------------------------------
# 远端录制目录轮询
# ---------------------------------------------------------------------------

class Archive(threading.Thread):
    """轮询 RK3588 当前录制目录的 meta.jsonl，拿到已封存分段"""

    def __init__(self, remote, root, interval=3.0):
        super().__init__(daemon=True)
        self.remote = remote
        self.root = root
        self.interval = interval
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.segments = []
        self.session = ''
        # 录制起点（绝对 UTC 秒）。优先取 meta 首行的 base_ms，没有则用目录名
        # 里的启动时刻（stage1_YYYYmmdd_HHMMSS 是 RK3588 本地墙钟）。
        self.base_wall = None
        self.camera_alarm = None
        self.error = ''
        # 最近一次拉取时：录制端已写到哪里（written_end=最后一帧包，
        # last_end=最后一个封存段的结束），用于估算直播延迟
        self.written_end = 0.0
        self.updated_t = 0.0
        self.sync_wall = 0.0       # 本次拉取时录制轴对应的绝对 UTC 秒
        self._version = 0
        self._snap = []
        self._snap_ver = -1

    def snapshot(self):
        with self.lock:
            if self._version == self._snap_ver:
                return self._snap
            self._snap = list(self.segments)
            self._snap_ver = self._version
            return self._snap

    def session_name(self):
        with self.lock:
            return self.session

    def last_end(self):
        with self.lock:
            return self.segments[-1]['end_s'] if self.segments else 0.0

    def base_ms(self):
        """本场录制起点对应的绝对 UTC 毫秒；未知返回 None"""
        with self.lock:
            return self.base_wall

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self._refresh()
            except Exception as e:
                with self.lock:
                    self.error = '%s: %s' % (type(e).__name__, e)
            self.stop_evt.wait(self.interval)

    def _refresh(self):
        # 按目录名（stage1_YYYYmmdd_HHMMSS）排序而非 mtime：目录名即启动时刻，
        # 不受后续写入影响；mtime 会被时钟漂移/旧目录续写带偏，选错会话。
        # 同时读取 camera_alarm.json：录制端连续取帧失败 30 秒会写该文件。
        # 同时取正在写的那一段（尚未进 meta 的 .h265）的 stat：
        # 录制端每 2 秒打一行进度日志，其中 dt 就是"已经写到第几秒"，
        # 用它和已封存段的结束时间比较，即可估出直播相对录制的延迟。
        cmd = ('d=$(ls -1d %s/stage1_* 2>/dev/null | sort | tail -1); '
               'if [ -n "$d" ]; then echo "DIR $d"; '
               'cat "$d/meta.jsonl" 2>/dev/null; '
               'echo "ALARM"; cat "$d/camera_alarm.json" 2>/dev/null; '
               'echo "PROG"; grep -o -E "[0-9]+(\\.[0-9]+)?s 取" '
               '/tmp/hk_record.log 2>/dev/null | tail -1; fi'
               ) % self.root
        # 记下"发出请求"与"收到结果"的中点：远端进度日志是 ssh 会话内当场
        # 读取的，其时刻≈中点；脚本开头 date +%s%N 让这份时刻精确到毫秒，
        # 不再受 ssh 建链（可长达 1~4s）误差影响。
        t0 = time.time()
        if self.remote is None:
            p = subprocess.run(
                ['bash', '-lc', 'date +%s%N; ' + cmd],
                capture_output=True, text=True, timeout=20)
        else:
            p = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                 self.remote, 'date +%s%N; ' + cmd],
                capture_output=True, text=True, timeout=20)
        t1 = time.time()
        mid = (t0 + t1) / 2.0
        lines = p.stdout.splitlines()
        try:
            remote_now = int(lines[0]) / 1e9
            lines = lines[1:]
        except (IndexError, ValueError):
            remote_now = mid
        session = ''
        segs = []
        alarm = None
        base = None
        prog_s = None
        in_alarm = False
        for line in lines:
            if line.startswith('DIR '):
                session = line[4:].strip()
            elif line.strip() == 'ALARM':
                in_alarm = True
            elif line.strip() == 'PROG':
                in_alarm = False
            elif in_alarm and line.strip().startswith('{'):
                try:
                    alarm = json.loads(line)
                except ValueError:
                    alarm = None
            elif prog_s is None and 's 取' in line:
                # 进度行形如 "  132.6s 取 7846 帧 59.2 FPS, ..."
                tok = line.strip().split('s 取')[0].strip()
                try:
                    prog_s = float(tok)
                except ValueError:
                    prog_s = None
            elif not in_alarm and line.startswith('{'):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rec['dir'] = session
                rec['start_s'] = rec['start_ms'] / 1000.0
                rec['end_s'] = rec['end_ms'] / 1000.0
                # base_ms = 该段第 0 帧的绝对 UTC 时间（录制端写入）。
                # 用它反推本场起点，比目录名精确到帧，且不受 NTP 跳变影响。
                b = rec.get('base_ms')
                if b is not None:
                    start = b - rec['start_ms']
                    if base is None or start < base:
                        base = start
                segs.append(rec)
        if base is None:
            base = _dir_wall_ms(session)
        # base_ms 是录制端亲手写的（每段第 0 帧的 UTC 时间，与 monotonic
        # 同刻配对），是唯一可信的绝对时间锚点——不要拿本机的 remote_now
        # 去"校准"它：ssh 的 `date` 包含建链/执行时间，录制端跑在别的机器/
        # 别的进程里也都对不齐。旧代码见"进度日志与末端差 5 秒以上"就整体
        # 平移 base，录制一停进度日志就停了，而 remote_now 还在走，于是每次
        # 轮询都把时钟往回拨 3 秒（直播画面断掉时时间码来回跳就是这么来的）。
        # 真正的时钟偏移要在 LIVE 头里按 PTS 对，而不是动 base。
        sync = None
        if base is not None:
            if prog_s is not None:
                # 进度日志说的"已采集到第 prog_s 秒"（录制轴）
                sync = base / 1000.0 + prog_s
            elif segs and remote_now:
                sync = remote_now
        with self.lock:
            if session:
                self.session = session
                self.segments = segs
                self.camera_alarm = alarm
                self.base_wall = base
                if prog_s is not None:
                    self.written_end = prog_s
                elif segs:
                    self.written_end = segs[-1]['end_s']
                self.updated_t = time.monotonic()
                self.sync_wall = sync or 0.0
                self._version += 1
                self.error = '' if p.returncode == 0 else p.stderr.strip()
            elif not p.stdout.strip():
                self.error = (p.stderr.strip() or '未找到录制目录')


    def segments_available(self):
        """远端当前是否真的有可回放的场次（区别于"还没轮询到"）"""
        with self.lock:
            return bool(self.segments)


def _dir_wall_ms(session):
    """从 stage1_YYYYmmdd_HHMMSS 目录名反推启动时刻（本地时区 → UTC 毫秒）"""
    if not session:
        return None
    base = os.path.basename(session.rstrip('/'))
    if not base.startswith('stage1_') or len(base) < 22:
        return None
    try:
        st = time.mktime(time.strptime(base[7:22], '%Y%m%d_%H%M%S'))
    except ValueError:
        return None
    # mktime 按本地时区解释；RK3588 与 DGX 现在都走 Asia/Shanghai
    return st * 1000.0


def fmt_clock(sec):
    """秒 → 比赛时钟 HH:MM:SS"""
    sec = max(0, int(sec))
    return '%02d:%02d:%02d' % (sec // 3600, sec % 3600 // 60, sec % 60)


def fmt_hms(ts):
    """绝对时间（epoch 秒）→ 本地 HH:MM:SS"""
    return time.strftime('%H:%M:%S', time.localtime(ts))


# ---------------------------------------------------------------------------
# 片段加载 / 缓存
# ---------------------------------------------------------------------------

class SegmentCorrupt(RuntimeError):
    """远端分段本身已损坏（空/截断/帧数不符），重拉无意义"""


class ClipLoader(threading.Thread):
    """按需拉取远端分段并转封装；缓存后可在段内任意定位"""

    def __init__(self, remote, cache_dir, cache_max_gb=20.0,
                 local_dirs=None):
        super().__init__(daemon=True)
        self.remote = remote      # None = 本机模式
        self.cache_dir = cache_dir
        self.cache_max_gb = cache_max_gb
        # 本机归档目录：归档成功后远端原文件会被删掉，此时再去 scp 必然
        # 失败。先在 <local_dir>/<场次>/<文件名> 找，命中就不用走网络。
        self.local_dirs = [d for d in (local_dirs or []) if d]
        os.makedirs(cache_dir, exist_ok=True)
        self.cv = threading.Condition()
        self.want = None          # 当前要拉的段（优先于队列）
        self.queue = []           # 待拉队列，先进先出
        self.clips = {}
        self.errors = {}
        self.bad = set()          # 确认损坏的段，不再尝试拉取
        self.fail_count = {}      # file -> 已重试次数，供 UI 显示进度
        self.loading = ''
        self._last_error = ''
        self.proc = None
        self.stop_evt = threading.Event()

    def mark_bad(self, name):
        with self.cv:
            self.bad.add(name)

    def is_bad(self, name):
        with self.cv:
            return name in self.bad

    def request(self, seg, idle_only=False):
        """idle_only=True 用于预取：只有完全空闲时才排队。
        非 idle_only 立即插到队首，优先于已排队的预取。"""
        key = seg['file']
        with self.cv:
            if key in self.clips or key in self.bad:
                return
            idle = self.want is None and not self.queue and not self.loading
            if idle_only and not idle:
                return
            if self.want is not None:
                self.queue.insert(0, self.want)   # 正在等的让位，别丢
            if idle_only:
                self.queue.append(dict(seg))
            else:
                self.want = dict(seg)             # 用户请求：插到最前
            self.cv.notify()

    def clip(self, name):
        with self.cv:
            return self.clips.get(name)

    def error(self, name):
        with self.cv:
            return self.errors.get(name)

    def clear_error(self, name):
        with self.cv:
            self.errors.pop(name, None)

    def failures(self, name):
        with self.cv:
            return self.fail_count.get(name, 0)

    def loading_name(self):
        with self.cv:
            return self.loading

    def stop(self):
        with self.cv:
            self.stop_evt.set()
            self.cv.notify()
            proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _next(self):
        """取下一个待拉段；优先 want，其次队列（去重、跳过已缓存）"""
        with self.cv:
            while True:
                if self.stop_evt.is_set():
                    return None
                seg = self.want
                self.want = None
                if seg is None:
                    while self.queue:
                        s = self.queue.pop(0)
                        if s['file'] not in self.clips:
                            seg = s
                            break
                    if seg is None:
                        self.cv.wait(0.5)
                        continue
                if seg['file'] in self.clips:
                    continue
                self.loading = seg['file']
                return seg

    # 段请求丢失 / 网卡停摆时不要无脑重试整个队列，只重试被用户要的段
    RETRY_DELAYS = (0.0, 2.0, 4.0, 8.0, 15.0)

    def run(self):
        while not self.stop_evt.is_set():
            seg = self._next()
            if seg is None:
                return
            name = seg['file']
            for attempt, delay in enumerate(self.RETRY_DELAYS):
                if delay and self.stop_evt.wait(delay):
                    return
                try:
                    info = self._ensure(seg)
                    if self.cache_max_gb > 0:
                        self._enforce_cache()
                    with self.cv:
                        self.clips[name] = info
                        self.errors.pop(name, None)
                        self.fail_count.pop(name, None)
                        # 成功必须立刻清掉 loading，否则下一轮 idle 判定
                        # 永远为 False，预取全被丢掉、段尾等不到下一段
                        self.loading = ''
                    break
                except Exception as e:
                    msg = '%s: %s' % (type(e).__name__, e)
                    with self.cv:
                        self.errors[name] = msg
                        self.fail_count[name] = attempt + 1
                        # 数据本身坏了（空/短/帧数不符）：重拉结果一样，不必重试。
                        # 但**只放弃这一段**，不能 return——退出去会把整个
                        # loader 线程杀掉，之后所有请求都无人处理、clip()
                        # 永远返回 None 且没有错误（表现为回放永久卡在
                        # "准备片段"，换别的场次也救不回来）。
                        if isinstance(e, SegmentCorrupt):
                            self.loading = ''
                            self._last_error = msg
                            break
                        # 网卡停摆常几十秒自愈：留 15s 兜底，别让请求永久失败
                        if attempt == len(self.RETRY_DELAYS) - 1:
                            self.loading = ''
                            self._last_error = msg

    def _local_src(self, seg):
        """本机能找到这一段的原文件时返回其路径；否则 None。

        先看段自己带的 dir（「打开录像」打开的任意目录走这条：dir 就是
        那个目录的绝对路径），再按归档约定 <local_dir>/<场次>/<文件名>
        找（与远端 `<remote>:<remote_root>/<场次>/<文件名>` 一一对应）。
        """
        if seg.get('local_dir'):
            p = os.path.join(seg['local_dir'], seg['file'])
            if os.path.isfile(p):
                return p
        sess = os.path.basename(seg['dir'].rstrip('/'))
        for d in self.local_dirs:
            p = os.path.join(d, sess, seg['file'])
            if os.path.isfile(p):
                return p
        return None

    def _ensure(self, seg):
        key = os.path.basename(seg['dir'].rstrip('/')) + '__' + seg['file']
        mkv = os.path.join(self.cache_dir, key[:-5] + '.mkv')
        dur = seg['end_s'] - seg['start_s']
        # 打开外部目录时 meta 可能没记帧数：数一遍包数，别拿猜的 fps 去
        # remux（帧数对不上会被判成损坏，白拉一整段）
        if seg.get('frames'):
            frames = seg['frames']
        else:
            local0 = self._local_src(seg)
            frames = probe_fps(local0) if local0 else None
            if frames:
                seg['frames'] = frames
        fps = frames / dur if (frames and dur > 0) else 59.94
        # 缓存里的 mkv 可能是一段**半截文件**：上次进程在 remux 途中被杀
        # （Ctrl-C、强退、断电），只留下 0 字节或截断的 .mkv。它存在就
        # 被当成"已缓存"永久复用，回放永远打不开这一幕。0 字节直接丢，
        # 远小于预期（按帧数×码率粗估）也当半截。
        if os.path.exists(mkv):
            try:
                have = os.path.getsize(mkv)
            except OSError:
                have = 0
            # 预期大小下限：至少每帧 100 字节（5MP HEVC 实际远高于此），
            # 拿它当"明显不完整"的判据，宁可多 remux 一次也不放坏缓存过去。
            floor = (frames or 1) * 100
            if have < floor:
                try:
                    os.remove(mkv)
                except OSError:
                    pass
        if not os.path.exists(mkv):
            part = mkv[:-4] + '.part.h265'
            remote = '%s:%s/%s' % (self.remote, seg['dir'], seg['file'])
            try:
                local = self._local_src(seg)
                if local is not None:
                    # 已归档到本机：直接复制，省掉一次网络往返。
                    # 远端那份归档后已经删了，scp 只会失败。
                    # 同一个文件就不必复制（打开的就是缓存目录本身）。
                    if os.path.abspath(local) != os.path.abspath(part):
                        shutil.copyfile(local, part)
                    err = ''
                elif self.remote is None:
                    # 本机模式但没有命中 _local_src：直接用段自带的绝对 dir。
                    srcp = os.path.join(seg['dir'], seg['file'])
                    if os.path.abspath(srcp) != os.path.abspath(part):
                        shutil.copyfile(srcp, part)
                    err = ''
                else:
                    with self.cv:
                        self.proc = subprocess.Popen(
                            ['scp', '-q', '-o', 'BatchMode=yes',
                             '-o', 'ConnectTimeout=6', remote, part],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
                        proc = self.proc
                    _, err = proc.communicate(timeout=180)
                    if proc.returncode != 0:
                        raise RuntimeError('scp failed %s' % err.strip())
                # 远端文件可能被中断写空/写短（录制异常退出、环形缓冲截断）。
                # 直接抛错而不是无限重试 remux：文件大小对不上就是数据坏了，重拉也一样。
                got = os.path.getsize(part) if os.path.exists(part) else 0
                want = seg.get('bytes') or 0
                if got == 0:
                    raise SegmentCorrupt('片段为空（远端 %d 字节，meta 记 %d）'
                                         % (got, want))
                if want > 0 and abs(got - want) > max(65536, want * 0.02):
                    raise SegmentCorrupt('片段大小不符（远端 %d，meta %d）'
                                         % (got, want))
                if not remux_mkv(part, mkv, seg['frames'], fps):
                    raise SegmentCorrupt('remux 帧数不符')
            finally:
                with self.cv:
                    self.proc = None
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
        return {'mkv': mkv, 'fps': fps,
                'start_s': seg['start_s'], 'frames': seg['frames']}

    def _enforce_cache(self):
        items = []
        for name in os.listdir(self.cache_dir):
            if not name.endswith('.mkv'):
                continue
            path = os.path.join(self.cache_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            items.append((st.st_mtime, st.st_size, path))
        total = sum(sz for _, sz, _ in items)
        limit = self.cache_max_gb * 1e9
        for _, sz, path in sorted(items):
            if total <= limit:
                break
            try:
                os.remove(path)
                total -= sz
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 远端控制
# ---------------------------------------------------------------------------

class RemoteCtrl:
    """启动/停止录制管线（直播源）。

    remote=None → 本机模式：管线和监看台在同一台 RK3588 上，直接起子进程，
                  不经 ssh。这是 RK3588 独立部署的目标形态。
    remote='xxx' → 远端模式：ssh 到另一台机器起管线（保留兼容）。
    """

    def __init__(self, remote, remote_root, segment=10.0, ring=90.0,
                 live_port=9101, min_free_gb=10.0, thumb_port=0):
        self.remote = remote
        self.remote_root = remote_root
        self.segment = segment
        self.ring = ring
        self.live_port = live_port
        # 缩略流端口：监看台只看缩略流，主码流全分辨率只留作落盘。
        # 四路 1224x1024@60 全推 DGX 会把解码压垮，缩略流实测 1.4 Mbps/路。
        self.thumb_port = thumb_port
        self.min_free_gb = min_free_gb

    @property
    def local(self):
        return self.remote is None

    def _ssh(self, cmd, timeout=20):
        if self.local:
            return subprocess.run(
                ['bash', '-lc', cmd],
                capture_output=True, text=True, timeout=timeout)
        return subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
             self.remote, cmd],
            capture_output=True, text=True, timeout=timeout)

    # 录制进程探针：只认 comm=python3 的进程，避免匹配到 ssh 远端 shell 自身
    _PROBE = ("ps -eo comm=,args= | awk '$1==\"python3\" && "
              "/hk_pipeline_rk[.]py/ {f=1} END {exit !f}'")

    def _start_cmd(self):
        # Only an explicit new recording resets the camera; an existing one is untouched.
        import shlex
        root = self.remote_root
        out = ('"$HOME"/' + shlex.quote(root[2:])) if root.startswith('~/') else shlex.quote(root)
        reason = '$(tail -n 4 /tmp/hk_record.log | tr "\\n" " ")'
        return (
            'if %s; then echo ALREADY; exit 0; fi; '
            '( . /etc/profile.d/mvs_env.sh 2>/dev/null; '
            'cd ~/hk_camera && exec setsid /usr/bin/python3 -u '
            'hk_pipeline_rk.py --reset-camera --seconds 0 --segment %g --ring %g '
            '--live-port %d --thumb-port %d --min-free-gb %g --out %s ) '
            '< /dev/null > /tmp/hk_record.log 2>&1 & rec_pid=$!; '
            'for i in $(seq 1 35); do sleep 1; '
            'if ! %s; then echo "START_FAILED: %s"; exit 0; fi; '
            'if grep -q "\\[lat\\]" /tmp/hk_record.log; then echo STARTED; exit 0; fi; '
            'done; kill -TERM "$rec_pid" 2>/dev/null; '
            'echo "START_FAILED: camera produced no encoded frames in time; %s"'
            % (self._PROBE, self.segment, self.ring, self.live_port,
               self.thumb_port, self.min_free_gb, out, self._PROBE, reason, reason))

    def _stop_cmd(self):
        # 不依赖 recorder.pid（实测该文件常不存在，导致本机点停止后
        # 脚本返回 NO_PID、录制照跑不停）。直接按进程命令行匹配，只认
        # comm=python3 且跑 hk_pipeline_rk.py 的进程，避免误杀 ssh 远端
        # shell 自身。SIGTERM 是管线自己的优雅退出路径（收尾写 meta、清 pid）。
        return ("pids=$(ps -eo comm=,pid=,args= | awk '$1==\"python3\" && "
                "/hk_pipeline_rk[.]py/ {print $2}'); "
                "if [ -z \"$pids\" ]; then echo NO_PROC; exit 0; fi; "
                "for p in $pids; do kill -TERM \"$p\" && echo \"STOPPING $p\"; "
                "done; "
                "for i in $(seq 1 30); do sleep 1; "
                # 用 kill -0 逐个探活。别用 `ps -p $pids`：$pids 为空时
                # 参数丢失会让 ps 列出全部进程，永远判成"还在跑"。
                "alive=0; for p in $pids; do kill -0 \"$p\" 2>/dev/null "
                "&& alive=1; done; "
                "[ \"$alive\" = 0 ] && echo STOPPED && exit 0; done; "
                "echo TIMEOUT")

    def start(self):
        """返回 (状态, 说明)，状态 in {'ALREADY','STARTED','FAIL'}

        STARTED 必须由后端复核进程存活后才会返回；进程起来就挂掉
        （典型：磁盘低于 --min-free-gb）返回 FAIL，说明里带拒绝原因。
        """
        p = self._ssh(self._start_cmd(), timeout=55)
        out = (p.stdout + '\n' + p.stderr).strip()
        if 'ALREADY' in p.stdout:
            return 'ALREADY', out
        if 'START_FAILED:' in p.stdout:
            reason = p.stdout.split('START_FAILED:', 1)[1].strip()
            return 'FAIL', reason or out
        if 'STARTED' in p.stdout:
            return 'STARTED', out
        return 'FAIL', out or 'rc=%d' % p.returncode

    def stop(self):
        """返回 ('STOPPED'|'NO_PROC'|'TIMEOUT'|'FAIL', 说明)"""
        p = self._ssh(self._stop_cmd(), timeout=55)
        out = (p.stdout + '\n' + p.stderr).strip()
        for k in ('STOPPED', 'NO_PROC', 'TIMEOUT'):
            if k in p.stdout:
                return k, out
        return 'FAIL', out or 'rc=%d' % p.returncode


# ---------------------------------------------------------------------------
# 场次归档
# ---------------------------------------------------------------------------

class Archiver:
    """把 RK3588 上的整场录像拉回本机目录，可选归档后删除远端原文件。

    设计约束（见 project_var_replay_system）：录制期间数据只落 RK3588 本地
    盘，绝不走网络——网络抖动会直接毁掉录像。因此「存在哪」只能是**事后**
    归档的目标，不能是录制时的写入位置。

    传输走 rsync（两端实测都有 3.2.7）：断点续传、失败重试都靠它，
    不必自己实现。先传到 `<目标>/.<场次>.partial`，校验文件数与字节数
    全部一致后再改名成 `<目标>/<场次>`——中途失败不会留下半场录像冒充
    完整场次。
    """

    def __init__(self, remote, remote_root):
        self.remote = remote
        self.remote_root = remote_root

    @property
    def local(self):
        return self.remote is None

    def _ssh(self, cmd, timeout=30):
        if self.local:
            return subprocess.run(
                ['bash', '-lc', cmd],
                capture_output=True, text=True, timeout=timeout)
        return subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
             self.remote, cmd],
            capture_output=True, text=True, timeout=timeout)

    def info(self, session):
        """查场次概况 → dict(segments, bytes, duration) 或 None（不存在）。"""
        # 空场次名会让 shell 里 d=<root>/ 恰好存在，返回空 SEG= → int('') 抛
        # ValueError。调用方可能拿到空串（如停止时场次还没轮询到），直接判无。
        if not session or not session.strip().strip('/'):
            return None
        sess = os.path.basename(session.rstrip('/'))
        # 注意：这里的 % 是 python 格式化占位符，shell 里的 $ 不受影响，
        # 但脚本内的 printf 式 % 必须写成 %% 才不会打架。
        cmd = ("d=%s/%s; [ -d \"$d\" ] || { echo NO_SESSION; exit 0; }; "
               "n=$(ls -1 \"$d\"/*.h265 2>/dev/null | wc -l); "
               "b=$(du -sb \"$d\" 2>/dev/null | cut -f1); "
               "k=$(grep -c '\"start_ms\"' \"$d\"/meta.jsonl 2>/dev/null); "
               "if [ -f \"$d/summary.json\" ]; then "
               "  s=$(/usr/bin/python3 -c \"import json;"
               "d=json.load(open('$d/summary.json'));"
               "print(float(d.get('seconds') or 0))\" 2>/dev/null); "
               "else s=0; fi; "
               "echo \"SEG=$n BYTES=$b ENTRIES=$k SECONDS=${s:-0}\""
               % (self.remote_root, sess))
        p = self._ssh(cmd)
        out = (p.stdout + p.stderr).strip()
        if 'NO_SESSION' in out:
            return None
        for line in out.splitlines():
            if line.startswith('SEG='):
                kv = dict(x.split('=', 1) for x in line.split())
                return {
                    'session': sess,
                    'segments': int(kv.get('SEG', 0)),
                    'bytes': int(kv.get('BYTES', 0)),
                    'entries': int(kv.get('ENTRIES', 0)),
                    'seconds': float(kv.get('SECONDS', 0) or 0),
                }
        return None

    def local_sessions(self, dest_dir):
        """本机归档目录里已有的场次名（新的在前）。

        归档默认会删掉 RK3588 原文件，之后 Archive 轮询就找不到场次了，
        回放只能从本地归档目录读——见 StudioController.local_archive_dir。
        """
        try:
            names = os.listdir(dest_dir)
        except OSError:
            return []
        out = [n for n in names
               if n.startswith('stage1_') and not n.startswith('.')
               and os.path.isfile(os.path.join(dest_dir, n, 'summary.json'))]
        out.sort(reverse=True)
        return out

    def load_local(self, sess_name, dest_dir):
        """把本机归档目录里的一个场次读成 sections 列表。

        归档删掉 RK3588 原文件后，远端已经没有这个场次了，但录像在本机
        依然是完整可回放的。读它的 meta.jsonl，字段口径与 _refresh 一致
        （dir/start_s/end_s/frames/bytes）。返回空列表表示没有可读的 meta。
        """
        sess_dir = os.path.join(dest_dir, sess_name)
        try:
            with open(os.path.join(sess_dir, 'meta.jsonl')) as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        segs = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rec['dir'] = sess_dir
                rec['start_s'] = rec['start_ms'] / 1000.0
                rec['end_s'] = rec['end_ms'] / 1000.0
            except (ValueError, KeyError):
                continue
            path = os.path.join(sess_dir, rec['file'])
            try:
                rec['bytes'] = os.path.getsize(path)
            except OSError:
                continue      # 文件都不在就别列出来了
            segs.append(rec)
        segs.sort(key=lambda r: r['start_s'])
        return segs

    def list_sessions(self, limit=10):
        """最近的场次目录名（新的在前）。"""
        p = self._ssh("ls -1d %s/stage1_* 2>/dev/null | sort | tail -%d"
                      % (self.remote_root, limit))
        return [os.path.basename(x.strip())
                for x in p.stdout.splitlines() if x.strip()]

    def archive(self, session, dest_dir, remove_remote=False,
                on_progress=None, cancel_evt=None):
        """归档一场录像。

        返回 (状态, 说明)，状态 in {'OK','CANCELED','FAIL'}。
        on_progress(done_bytes, total_bytes, phase) 可用来更新进度条，
        phase in {'connect','transfer','verify','rename','cleanup'}。
        """
        sess = os.path.basename(session.rstrip('/'))
        def prog(done, total, phase):
            if on_progress:
                try:
                    on_progress(done, total, phase)
                except Exception:
                    pass

        info = self.info(sess)
        if info is None:
            return 'FAIL', '远端找不到场次 %s' % sess
        total = info['bytes']
        if total <= 0:
            return 'FAIL', '场次 %s 是空的（0 字节）' % sess

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            return 'FAIL', '无法创建目标目录: %s' % e
        if not os.access(dest_dir, os.W_OK):
            return 'FAIL', '目标目录不可写: %s' % dest_dir

        final = os.path.join(dest_dir, sess)
        part = os.path.join(dest_dir, '.' + sess + '.partial')
        # 半截目录留着就续传；完整目录已存在则直接判过
        if os.path.isdir(final) and os.path.exists(
                os.path.join(final, 'summary.json')):
            return 'OK', '已存在于 %s' % final
        if os.path.isfile(final):
            return 'FAIL', '目标路径被同名文件占用: %s' % final

        if self.local:
            # 本机模式：源码和目标在同一台 RK3588 上。若同盘直接 rename，
            # 跨盘退化为 shutil.move（自动复制+删除）。不再走 rsync/scp。
            return self._archive_local(sess, dest_dir, final, part,
                                       total, info, remove_remote, prog,
                                       cancel_evt)

        src = '%s:%s/%s/' % (self.remote, self.remote_root, sess)
        # --info=progress2 的百分比基于总量，适合直接喂进度条；
        # -a 保留时间戳，--partial-dir 让中断后能接着传。
        cmd = ['rsync', '-a', '--info=progress2', '--partial',
               '--partial-dir=.rsync-partial',
               '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=6',
               src, part + '/']
        prog(0, total, 'connect')
        err_tail = []
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 bufsize=1)
        except OSError as e:
            return 'FAIL', '无法启动 rsync: %s' % e

        # rsync 的行是 \r 分隔的进度，逐段解析出「已传字节」
        buf = ''
        while True:
            if cancel_evt is not None and cancel_evt.is_set():
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                return 'CANCELED', '已取消（已传部分保留，下次可续传）'
            chunk = p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while '\r' in buf or '\n' in buf:
                i = buf.find('\r')
                j = buf.find('\n')
                if i < 0 or (j >= 0 and j < i):
                    i = j
                line, buf = buf[:i], buf[i + 1:]
                line = line.strip()
                if not line:
                    continue
                # 形如 "1,234,567  39%  45.67MB/s  0:01:02"
                head = line.split()
                if head and head[0].replace(',', '').isdigit():
                    done = int(head[0].replace(',', ''))
                    prog(min(done, total), total, 'transfer')
                elif not line.startswith('#'):
                    err_tail.append(line)
                    del err_tail[:-5]
        p.wait()
        prog(total, total, 'transfer')
        if p.returncode != 0:
            return 'FAIL', 'rsync rc=%d %s' % (
                p.returncode, ' / '.join(err_tail[-3:]) or '')
        if cancel_evt is not None and cancel_evt.is_set():
            return 'CANCELED', '已取消'

        # 校验：段数 + 字节数都要对得上，否则不认
        prog(total, total, 'verify')
        n_local = 0
        b_local = 0
        for root, _dirs, files in os.walk(part):
            for fn in files:
                if fn.endswith('.h265'):
                    n_local += 1
                try:
                    b_local += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        if n_local != info['segments']:
            return 'FAIL', '校验失败：本地 %d 段，远端 %d 段' % (
                n_local, info['segments'])
        # rsync 目标目录本身可能带一层 .rsync-partial，用总字节兜底
        if abs(b_local - total) > max(4096, total * 0.005):
            return 'FAIL', '校验失败：本地 %.1fMB，远端 %.1fMB' % (
                b_local / 1e6, total / 1e6)

        prog(total, total, 'rename')
        try:
            if os.path.isdir(final):
                # 空壳目录（只有我们建过的），清掉再改名
                try:
                    os.rmdir(final)
                except OSError:
                    return 'FAIL', '目标已存在且非空: %s' % final
            os.rename(part, final)
        except OSError as e:
            return 'FAIL', '改名失败: %s' % e

        note = '已归档到 %s（%d 段 / %.1f MB）' % (
            final, n_local, total / 1e6)
        if remove_remote:
            prog(total, total, 'cleanup')
            try:
                p2 = self._ssh('rm -rf %s/%s && echo REMOVED'
                               % (self.remote_root, sess), timeout=60)
                if 'REMOVED' not in p2.stdout:
                    return 'OK', note + '；但删除 RK3588 原文件失败，请手工清理'
                note += '；RK3588 原文件已删除（本场回放改读本地归档）'
            except Exception as e:
                return 'OK', note + '；删除 RK3588 原文件失败: %s' % e
        return 'OK', note

    def _archive_local(self, sess, dest_dir, final, part, total, info,
                       remove_remote, prog, cancel_evt):
        """本机模式归档：源和目标都在本机，用移动代替网络传输。

        同盘 rename 瞬时完成；跨盘 shutil.move 会复制后删除源目录。
        先移到 .partial 再改名，保持与远端模式一致的"原子完成"语义。
        """
        src_dir = os.path.join(self.remote_root, sess)
        prog(0, total, 'connect')
        if not os.path.isdir(src_dir):
            return 'FAIL', '找不到场次目录 %s' % src_dir
        try:
            if os.path.isdir(part):
                shutil.rmtree(part, ignore_errors=True)
            prog(0, total, 'transfer')
            shutil.move(src_dir, part)
            prog(total, total, 'transfer')

            prog(total, total, 'verify')
            n_local = 0
            b_local = 0
            for root, _dirs, files in os.walk(part):
                for fn in files:
                    if fn.endswith('.h265'):
                        n_local += 1
                    try:
                        b_local += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
            if n_local != info['segments']:
                return 'FAIL', '校验失败：本地 %d 段，源 %d 段' % (
                    n_local, info['segments'])
            if abs(b_local - total) > max(4096, total * 0.005):
                return 'FAIL', '校验失败：本地 %.1fMB，源 %.1fMB' % (
                    b_local / 1e6, total / 1e6)

            prog(total, total, 'rename')
            if os.path.isdir(final):
                try:
                    os.rmdir(final)
                except OSError:
                    return 'FAIL', '目标已存在且非空: %s' % final
            os.rename(part, final)
        except OSError as e:
            return 'FAIL', '本机归档失败: %s' % e

        note = '已归档到 %s（%d 段 / %.1f MB）' % (
            final, n_local, total / 1e6)
        if remove_remote:
            # 本机模式下 src_dir 已被 move 走，无需再删。
            note += '（源目录已移走）'
        return 'OK', note


# ---------------------------------------------------------------------------
# 录像库
# ---------------------------------------------------------------------------

def fmt_size(n):
    """字节 → 人类可读（GB/MB/KB）"""
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return '%.1f TB' % n


class Library:
    """本机录像库：列场次、读标记、删场次。

    刻意**不读分段内容**：一场 644 段时逐段 stat 要几百毫秒，
    列表就会卡。只读 meta.jsonl 的行（录制端写的分段记录）+
    summary.json（若归档时生成）+ 文件大小。实测 1456 段场次
    扫描约 30ms，可以直接在主线程调，但 UI 仍是后台线程调用。

    删除只允许删 roots 里的场次目录：roots 是本机归档目录与用户
    手动选过的目录，避免一次手滑删掉 /home 或系统目录。
    """

    def __init__(self, roots, marks_path=None):
        # roots 里可能重复（归档目录 == 上次打开目录），保持顺序去重
        seen = []
        for r in roots:
            r = os.path.abspath(os.path.expanduser(r)) if r else None
            if r and r not in seen:
                seen.append(r)
        self.roots = seen
        # 总表 marks.jsonl：旧场次（未归档）的标记还留在这里
        self.marks_path = marks_path

    def list_sessions(self):
        """列出所有场次，新的在前。返回 dict 列表。"""
        out = []
        for root in self.roots:
            out.extend(self._scan_root(root))
        # 同一场次可能同时出现在多个 root（归档目录 + 手动打开目录），
        # 按绝对路径去重，保留先出现的那个（roots 顺序 = 用户优先级）。
        seen = set()
        uniq = []
        for s in out:
            if s['dir'] in seen:
                continue
            seen.add(s['dir'])
            uniq.append(s)
        uniq.sort(key=lambda s: (s['name']), reverse=True)
        return uniq

    def _scan_root(self, root):
        try:
            names = os.listdir(root)
        except OSError:
            return []
        out = []
        for n in names:
            if n.startswith('.'):
                continue
            d = os.path.join(root, n)
            meta = os.path.join(d, 'meta.jsonl')
            if not os.path.isfile(meta):
                continue
            info = self._read_info(d, n, meta)
            if info is not None:
                out.append(info)
        return out

    def _read_info(self, d, name, meta):
        """读一场的概况。meta 里最后一段的 end_ms 就是本场时长。"""
        try:
            with open(meta, encoding='utf-8') as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        segs = 0
        dur = 0.0
        total_bytes = 0
        missing = 0
        for line in lines:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                rec = json.loads(line)
                end_ms = float(rec['end_ms'])
            except (ValueError, KeyError, TypeError):
                continue
            segs += 1
            # 录制端写的 start_ms/end_ms 是**相对本场起点**的毫秒数；
            # 但手工拼的场次可能写成绝对 epoch（带 base_ms）。有 base_ms
            # 且 end_ms 明显大于 base_ms 时按绝对时间算，否则直接用相对值。
            b = rec.get('base_ms')
            if b is not None and end_ms > float(b):
                end_ms -= float(b)
            if end_ms > dur:
                dur = end_ms / 1000.0
            # 文件大小失败 = 分段文件不在了，标记为不完整
            try:
                total_bytes += os.path.getsize(os.path.join(d, rec['file']))
            except (OSError, KeyError):
                missing += 1
        if not segs:
            return None
        # 录制起始时刻：有 base_ms 就是绝对 epoch，否则退回目录 mtime
        try:
            st = os.stat(d)
            mtime = st.st_mtime
        except OSError:
            mtime = 0.0
        return {
            'name': name,
            'dir': d,
            'root': os.path.dirname(d),
            'segments': segs,
            'seconds': dur,
            'bytes': total_bytes,
            'missing': missing,
            'mtime': mtime,
            'marks': self.count_marks(d, name),
        }

    def count_marks(self, session_dir, name):
        """本场标记条数（场次目录自带 marks.jsonl 优先）。"""
        n = 0
        p = os.path.join(session_dir, 'marks.jsonl')
        if os.path.isfile(p):
            n += self._count_mark_lines(p, name)
        if self.marks_path and os.path.isfile(self.marks_path):
            n += self._count_mark_lines(self.marks_path, name)
        return n

    @staticmethod
    def _count_mark_lines(path, name):
        """数有效标记行：只认有 t_s 的，并过滤 session 不匹配的记录。"""
        n = 0
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith('{'):
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if 't_s' not in d:
                        continue
                    if d.get('op') in ('note', 'shift'):
                        continue
                    sess = d.get('session')
                    if sess and sess != name:
                        continue
                    n += 1
        except OSError:
            pass
        return n

    def delete_session(self, session_dir):
        """删掉一个场次目录，返回释放的字节数。

        三重闸门：路径必须**直接位于**某个 root 下（不能是 root 自身、
        不能靠 .. 跳出去）、目录里必须有 meta.jsonl（确认是场次而不是
        别的目录）。只有全部通过才 rm -rf。
        """
        d = os.path.abspath(os.path.expanduser(session_dir or ''))
        if not d or d in ('/', os.path.expanduser('~')):
            raise ValueError('拒绝删除：路径不安全')
        parent = os.path.dirname(d)
        if parent not in self.roots:
            raise ValueError('拒绝删除：%s 不在录像库目录内' % parent)
        if not os.path.isfile(os.path.join(d, 'meta.jsonl')):
            raise ValueError('拒绝删除：%s 不是场次目录' % d)
        # 先统计大小，删完就没有了
        total = 0
        for root_, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root_, fn))
                except OSError:
                    pass
        shutil.rmtree(d)
        return total

    def read_marks(self, session_dir, name):
        """读出本场标记，按 t_s 升序。供录像库详情面板预览。

        解析规则与 StudioController._load_marks 保持一致：note/shift 是
        对已有标记的追加修改，按 (wall, pre_s, note 首词) 合并。
        """
        paths = [os.path.join(session_dir, 'marks.jsonl')]
        if self.marks_path and self.marks_path not in paths:
            paths.append(self.marks_path)
        by_key = {}
        order = []
        for src in paths:
            try:
                with open(src, encoding='utf-8') as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            is_overview = (src == self.marks_path)
            for ln in lines:
                ln = ln.strip()
                if not ln.startswith('{'):
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(d, dict) or 't_s' not in d:
                    continue
                # 总表里没写 session 的是旧版本遗留，一律丢弃
                if d.get('session', '' if is_overview else name) != name:
                    continue
                k = (d.get('wall'), d.get('pre_s'),
                     d.get('note', '').split(' ')[0])
                if d.get('op') in ('note', 'shift'):
                    base = by_key.get(k)
                    if base is not None:
                        if d['op'] == 'note':
                            base['note'] = d['note']
                        else:
                            base['t_s'] = d['t_s']
                    continue
                if k in by_key:
                    continue
                by_key[k] = d
                order.append(k)
        marks = [by_key[k] for k in order]
        marks.sort(key=lambda m: m.get('t_s') or 0.0)
        return marks
