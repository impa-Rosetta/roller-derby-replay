#!/usr/bin/env python3
# RK3588 only, 2026-11-19: FFmpeg hevc_rkmpp CBR replaces GStreamer MPP.
# Original camera / segment / replay / SLIVE1 interfaces are retained.
# -*- coding: utf-8 -*-
"""RK3588 camera recorder: CPU Bayer conversion -> NV12 -> FFmpeg hevc_rkmpp.

The installed GStreamer MPP path stalled at 45-49 fps and reported RGA errors.
FFmpeg's MPP encoder is run in a child process, with CBR and NUT AU framing.
Captured monotonic timestamps travel in a FIFO, preserving recording/replay PTS.
Segmented Annex-B HEVC, ring replay and SLIVE1 live transport remain compatible.
Default full-resolution live output is port 9101. Optional thumbnail encoding is
not enabled in this implementation (--thumb-port must remain 0).
SIGTERM/SIGINT stop capture, drain encoding and close the final recording segment.
"""
import argparse
import ctypes as C
import json
import os
import queue
import shutil
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path
import resource
import subprocess
import av
from collections import deque

import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

import cv2  # noqa: E402
FRAME_NS = int(1e9 / 60)
# 缩略流每 N 帧送一帧（60fps 主链路 → 10fps 缩略流）。
# 两路 mpph265enc 满载会抢 MPP 硬件，拖垮主链路，必须降频。
THUMB_EVERY = 6
# 相机 BayerGR8 的相位：实测 GBRG 才对（肤色自然、衣物灰黑）。
# 注意 OpenCV 的命名与相机/GenICam 的命名相反：相机说 BayerGR8，
# OpenCV 要用 COLOR_BayerGB2BGR。四个相位都试过，只有这个不偏色：
#   GRBG→人像发蓝  GBRG→正常  RGGB/BGGR→灰阶近似相等（明显错）
DEMOSAIC = cv2.COLOR_BayerGB2BGR
# 多线程设置：OpenCV 默认按 CPU 核数并行，RK3588 有 8 核。
cv2.setNumThreads(8)

_stop = False


def _on_term(_sig, _frame):
    global _stop
    _stop = True


def preprocess(gray, size=None):
    """Bayer 灰度 → BGR → I420（BT.601 limited range）

    全部用 OpenCV 在 CPU 上完成。RK3588 的 RGA 不支持 Bayer 格式，只能走
    这里；实测去马赛克 1.1ms + 转 YUV 1.1ms，60fps 预算 16.67ms 足够。

    size=(w,h) 时顺带缩放（缩略流用），缩放在去马赛克之后做，损失最小。
    """
    bgr = cv2.cvtColor(gray, DEMOSAIC)
    if size is not None:
        bgr = cv2.resize(bgr, size, interpolation=cv2.INTER_AREA)
    i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
    return bgr, i420


def reset_usb_camera(timeout=25.0):
    """Reset camera firmware before recording; never during active capture.

    SDK acceptance is not readiness: wait for physical USB3 re-enumeration.
    """
    devices = MV_CC_DEVICE_INFO_LIST()
    rc = MvCamera.MV_CC_EnumDevices(MV_USB_DEVICE, devices)
    if rc or devices.nDeviceNum != 1:
        raise RuntimeError('USB reset requires exactly one connected USB camera')
    info = C.cast(devices.pDeviceInfo[0], C.POINTER(MV_CC_DEVICE_INFO)).contents
    serial = bytes(info.SpecialInfo.stUsb3VInfo.chSerialNumber).split(b'\0',1)[0].decode(errors='replace')
    cam = MvCamera()
    created = opened = False
    try:
        rc = cam.MV_CC_CreateHandle(info)
        if rc:
            raise RuntimeError('Reset CreateHandle failed %#x' % (rc & 0xffffffff))
        created = True
        rc = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if rc:
            raise RuntimeError('Reset OpenDevice failed %#x (camera may be busy)' % (rc & 0xffffffff))
        opened = True
        rc = cam.MV_CC_SetCommandValue('DeviceReset')
        if rc:
            raise RuntimeError('DeviceReset rejected %#x' % (rc & 0xffffffff))
        print('[usb] Camera %s reset accepted; waiting for USB3 re-enumeration' % serial, flush=True)
    finally:
        if opened:
            cam.MV_CC_CloseDevice()
        if created:
            cam.MV_CC_DestroyHandle()
    time.sleep(2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _stop:
        for node in Path('/sys/bus/usb/devices').iterdir():
            try:
                if (node/'idVendor').read_text().strip() != '2bdf':
                    continue
                if (node/'serial').read_text().strip() != serial:
                    continue
                if float((node/'speed').read_text()) < 5000:
                    raise RuntimeError('Camera reconnected below USB3 speed')
            except (OSError, ValueError):
                continue
            time.sleep(1.0)
            print('[usb] Camera %s USB3 re-enumeration ready' % serial, flush=True)
            return
        time.sleep(.5)
    raise RuntimeError('Camera did not re-enumerate as USB3 after DeviceReset')


def open_camera():
    dev_list = MV_CC_DEVICE_INFO_LIST()
    MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dev_list)
    dev_info = None
    for i in range(dev_list.nDeviceNum):
        info = dev_list.pDeviceInfo[i].contents
        if info.nTLayerType == MV_USB_DEVICE:
            dev_info = info
            break
    if dev_info is None:
        raise RuntimeError('没有找到 USB 相机')

    cam = MvCamera()
    cam.MV_CC_CreateHandle(dev_info)
    if cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0) != 0:
        cam.MV_CC_DestroyHandle()
        raise RuntimeError('打开相机失败')

    cam.MV_CC_SetEnumValue('TriggerMode', MV_TRIGGER_MODE_OFF)
    if cam.MV_CC_SetEnumValue('PixelFormat', PixelType_Gvsp_BayerGR8) != 0:
        raise RuntimeError('相机不支持 BayerGR8')
    # ---- 曝光控制（2026-11-10 重写：原写法全程静默失败）----
    # 旧代码用 SetFloatValue 写 AutoExposureTimeUpperLimit，但它是 **INT
    # 节点**，float 写返回 0x80000109 失败 → 上限从来就没生效过。AE 于是
    # 无约束往上爬，暗光下爬到 15816us（超过 59.2fps 的 16.9ms 帧间隔），
    # 帧率掉到 1fps，取帧 5 次 1s 超时全失败 → RuntimeError('取帧失败')
    # 自杀。现象是"直播放不起来"，极易误判成 USB/xHCI 假死。
    # 实测（现场光照）：旧写法 0.0fps｜AE + INT 上限 10000 → 59.5fps｜
    # 纯手动固定 10000 → 59.5fps。选 AE + 硬上限：暗光仍自适应但不越界。
    # 另注意：AE 开着时 ExposureTime 是只读的（写返回 0x80000106），
    # 要手动固定必须先 SetEnumValue('ExposureAuto', 0)。
    EXP_MAX_US = 10000
    cam.MV_CC_SetFloatValue('AutoExposureTimeLowerLimit', 100.0)
    rc_up = cam.MV_CC_SetIntValueEx('AutoExposureTimeUpperLimit', EXP_MAX_US)
    # 开机先按手动固定一次，清掉上一次会话可能残留的越界曝光值
    cam.MV_CC_SetEnumValue('ExposureAuto', 0)
    rc_fix = cam.MV_CC_SetFloatValue('ExposureTime', float(EXP_MAX_US))
    rc_ae = cam.MV_CC_SetEnumValue('ExposureAuto', 2)
    print('曝光: 上限=%dus(rc=%d) 初始=%.0fus(rc=%d) AE(rc=%d)'
          % (EXP_MAX_US, rc_up, EXP_MAX_US, rc_fix, rc_ae))
    if rc_up != 0:
        # 上限写不进去就别交给 AE：关掉自动，永久固定曝光。
        # 宁可失去自适应，也不能让它把帧率拖垮到不可用。
        print('WARN: AE 上限设置失败，保持手动固定曝光')
        cam.MV_CC_SetEnumValue('ExposureAuto', 0)
    elif rc_ae != 0:
        print('WARN: 无法恢复 AE，保持手动固定曝光')

    cam.MV_CC_SetEnumValue('GainAuto', 2)
    cam.MV_CC_SetEnumValue('BalanceWhiteAuto', 2)
    cam.MV_CC_SetBoolValue('AcquisitionFrameRateEnable', True)
    cam.MV_CC_SetFloatValue('AcquisitionFrameRate', 60.0)

    # ---- 图像节点深度（2026-11-18 实测）----
    # 默认 nImageNodeNum=1 时，GetOneFrameTimeout 平均要阻塞 10.4ms 才拿到
    # 一帧——可相机明明 59fps 出帧（间隔 16.9ms），说明帧早就在驱动里了，
    # 是 SDK 节点太浅导致"等下一帧就绪"的额外等待。改成 8 之后取帧耗时
    # 变成 16.88ms 均值 / p50 16.87 / max 17.50，200 帧 3.38s = 59.3fps，
    # 丢帧 0，即取帧精确按帧间隔推进。内存代价 8×5MB≈40MB，可接受。
    rc_node = cam.MV_CC_SetImageNodeNum(8)
    print('图像节点深度=8 (rc=%d)' % rc_node)

    st = MVCC_INTVALUE_EX()
    C.memset(C.byref(st), 0, C.sizeof(MVCC_INTVALUE_EX))
    cam.MV_CC_GetIntValueEx('PayloadSize', st)
    payload = st.nCurValue

    tick = 0
    st2 = MVCC_INTVALUE_EX()
    C.memset(C.byref(st2), 0, C.sizeof(MVCC_INTVALUE_EX))
    if cam.MV_CC_GetIntValueEx('TimestampTickFrequency', st2) == 0:
        tick = st2.nCurValue
    return cam, payload, tick


def scan_nals(data, types):
    """按起始码扫描 AU，返回命中 types 的 NAL 字节（含起始码）"""
    out = []
    i = 0
    n = len(data)
    while i < n - 4:
        j = data.find(b'\x00\x00\x01', i)
        if j < 0 or j + 3 >= n:
            break
        k = data.find(b'\x00\x00\x01', j + 3)
        end = k if k >= 0 else n
        if ((data[j + 3] >> 1) & 0x3F) in types:
            out.append(data[j:end])
        i = end if k >= 0 else n
    return out


def iter_nal_types(data):
    """遍历 Annex-B 数据中的 NAL 类型"""
    i = 0
    n = len(data)
    while i < n - 4:
        j = data.find(b'\x00\x00\x01', i)
        if j < 0 or j + 3 >= n:
            break
        yield (data[j + 3] >> 1) & 0x3F
        i = j + 3


def has_irap(data):
    """裸流 AU 中是否含 IRAP（IDR/CRA）NAL"""
    return any(16 <= t <= 21 for t in iter_nal_types(data))


def has_idr(data):
    """裸流 AU 中是否含 IDR（NAL 19）：直播从 IDR 起播最干净"""
    return any(t == 19 for t in iter_nal_types(data))


class SegmentWriter:
    """把编码后 AU 追加写入 .h265；仅在 IRAP 处切段，保证每段可独立解码"""

    def __init__(self, out_dir, seg_seconds, prefix='seg'):
        self.dir = out_dir
        self.seg_ns = int(seg_seconds * 1e9)
        self.prefix = prefix
        self.lock = threading.Lock()
        self.fh = None
        self.idx = 0
        self.seg_start_pts = 0
        self.seg_frames = 0
        self.seg_bytes = 0
        self.segments = []
        self.total_frames = 0
        self.total_bytes = 0
        self.keyframes = 0
        self.started = False
        self.param_sets = b''   # 最近一次收到的 VPS/SPS/PPS，段首补齐用
        self.meta = open(os.path.join(out_dir, 'meta.jsonl'), 'w')

    def _open(self, pts_ns, wall, wall_ns=None):
        name = '%s_%03d_%s.h265' % (self.prefix, self.idx,
                                    time.strftime('%H%M%S', wall))
        path = os.path.join(self.dir, name)
        self.fh = open(path, 'wb')
        self.path = path
        self.seg_start_pts = pts_ns
        self.seg_base_wall = wall_ns
        self.seg_frames = 0
        self.seg_bytes = 0

    def _close(self, end_pts_ns):
        if self.fh is None:
            return
        self.fh.close()
        rec = {'file': os.path.basename(self.path),
               'start_ms': round(self.seg_start_pts / 1e6, 1),
               'end_ms': round(end_pts_ns / 1e6, 1),
               'frames': self.seg_frames,
               'bytes': self.seg_bytes}
        if self.seg_base_wall is not None:
            # 该段第 0 帧的绝对 UTC 时间（ms）：监看台据此换算比赛时钟
            rec['base_ms'] = self.seg_base_wall / 1e6
        self.meta.write(json.dumps(rec) + '\n')
        self.meta.flush()
        self.segments.append(rec)
        self.idx += 1
        self.fh = None

    def _write_head(self, data):
        """段首若缺参数集（编码器只在 IDR 前插入），补上缓存的 VPS/SPS/PPS"""
        if self.param_sets and not scan_nals(data, (32, 33, 34)):
            self.fh.write(self.param_sets)
            self.seg_bytes += len(self.param_sets)

    def write(self, data, pts_ns, is_irap, wall_ns=None):
        with self.lock:
            params = scan_nals(data, (32, 33, 34))
            if params:
                self.param_sets = b''.join(params)
            if self.fh is None:
                self._open(pts_ns, time.localtime(), wall_ns)
                self._write_head(data)
            elif is_irap and pts_ns - self.seg_start_pts >= self.seg_ns:
                self._close(pts_ns)
                self._open(pts_ns, time.localtime(), wall_ns)
                self._write_head(data)
            self.fh.write(data)
            self.seg_frames += 1
            self.seg_bytes += len(data)
            self.total_frames += 1
            self.total_bytes += len(data)
            if is_irap:
                self.keyframes += 1

    def close(self, end_pts_ns=0):
        with self.lock:
            self._close(end_pts_ns or self.seg_start_pts)
            self.meta.close()


class RingBuffer:
    """最近 N 秒编码 AU 的内存环形缓冲（GOP 对齐，可随时切片回放）

    只保存引用，不复制码流；淘汰时整 GOP 丢弃，保证缓冲开头总是 IRAP。
    """

    def __init__(self, seconds, max_mb=800):
        self.ring_ns = int(seconds * 1e9)
        self.max_bytes = int(max_mb * 1e6)
        self.buf = deque()          # (pts_ns, is_irap, bytes)
        self.nbytes = 0
        self.param_sets = b''
        self.lock = threading.Lock()
        self.fetches = 0

    def append(self, data, pts_ns, is_irap):
        with self.lock:
            if is_irap:
                params = scan_nals(data, (32, 33, 34))
                if params:
                    self.param_sets = b''.join(params)
            self.buf.append((pts_ns, is_irap, data))
            self.nbytes += len(data)
            last = self.buf[-1][0]
            while len(self.buf) > 1:
                too_old = last - self.buf[0][0] > self.ring_ns
                too_big = self.nbytes > self.max_bytes
                if not (too_old or too_big):
                    break
                idx = next((i for i in range(1, len(self.buf))
                            if self.buf[i][1]), None)
                if idx is None:
                    break
                for _ in range(idx):
                    _, _, d = self.buf.popleft()
                    self.nbytes -= len(d)

    def snapshot(self, seconds):
        """取最近 seconds 秒（从 <= 起点的最新 IRAP 开始），返回 (header, 参数集, AU 列表)"""
        with self.lock:
            if not self.buf:
                return None
            last = self.buf[-1][0]
            want = last - int(seconds * 1e9)
            start = None
            for i, (pts, irap, _) in enumerate(self.buf):
                if irap and pts <= want:
                    start = i                     # 取最新的、不晚于起点的 IRAP
            if start is None:
                start = next((i for i, au in enumerate(self.buf) if au[1]), 0)
            aas = list(self.buf)[start:]
            first_pts = aas[0][0]
            params = b''
            if not scan_nals(aas[0][2], (32, 33, 34)):
                params = self.param_sets
            payload = sum(len(au[2]) for au in aas) + len(params)
            hdr = struct.pack('<4sIQQIQ', b'HKR1', seconds,
                              first_pts // 1000000, last // 1000000,
                              len(aas), payload)
            self.fetches += 1
            return hdr, params, aas

    def info(self):
        with self.lock:
            if not self.buf:
                return 0.0, 0.0
            span = (self.buf[-1][0] - self.buf[0][0]) / 1e9
            return span, self.nbytes / 1e6


class ReplayServer(threading.Thread):
    """TCP 回放服务：客户端发 u32 秒数，服务端回 36 字节头 + 裸流"""

    def __init__(self, ring, port, host='::'):
        super().__init__(daemon=True)
        self.ring = ring
        self.port = port
        self.host = host
        self.stop_evt = threading.Event()
        self.srv = None

    def stop(self):
        self.stop_evt.set()
        s = self.srv
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def run(self):
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(2)
            srv.settimeout(0.5)
        except OSError as e:
            print('[replay] 监听失败: %s' % e)
            return
        self.srv = srv
        print('[replay] 回放服务监听 [%s]:%d' % (self.host, self.port), flush=True)
        while not self.stop_evt.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn, addr),
                             daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass

    def _serve(self, conn, addr):
        t0 = time.perf_counter()
        try:
            conn.settimeout(10.0)
            req = b''
            while len(req) < 4:
                chunk = conn.recv(4 - len(req))
                if not chunk:
                    return
                req += chunk
            seconds = struct.unpack('<I', req)[0]
            snap = self.ring.snapshot(seconds)
            if snap is None:
                print('[replay] 请求 %ds 但缓冲为空' % seconds, flush=True)
                return
            hdr, params, aas = snap
            conn.settimeout(30.0)
            conn.sendall(hdr)
            sent = len(hdr)
            if params:
                conn.sendall(params)
                sent += len(params)
            for _, _, data in aas:
                conn.sendall(data)
                sent += len(data)
            dt = time.perf_counter() - t0
            dur = (aas[-1][0] - aas[0][0]) / 1e9
            print('[replay] %s 请求 %ds → 实际 %.1fs %d 帧 %.1fMB，%.1fs 发完 (%.1f MB/s)'
                  % (addr[0], seconds, dur, len(aas), sent / 1e6, dt,
                     sent / dt / 1e6), flush=True)
        except (OSError, socket.timeout) as e:
            print('[replay] 客户端异常: %s' % e, flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass


class LiveServer(threading.Thread):
    """实时直播：客户端从下一个 IDR 开始接收编码 AU（裸 HEVC 连续流）"""

    def __init__(self, port, host='::', queue_len=240):
        super().__init__(daemon=True)
        self.port = port
        self.host = host
        self.queue_len = queue_len
        self.stop_evt = threading.Event()
        self.srv = None
        self.lock = threading.Lock()
        self.clients = {}       # conn -> Queue
        self.param_sets = b''

    def stop(self):
        self.stop_evt.set()
        s = self.srv
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def push(self, data, pts_ns=0):
        """编码回调里调用：把 AU 放进每个客户端队列（满了丢最旧）"""
        params = scan_nals(data, (32, 33, 34))
        with self.lock:
            if params:
                self.param_sets = b''.join(params)
            qs = list(self.clients.values())
        for q in qs:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait((data, pts_ns))
            except queue.Full:
                pass

    def run(self):
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(2)
            srv.settimeout(0.5)
        except OSError as e:
            print('[live] 监听失败: %s' % e)
            return
        self.srv = srv
        print('[live] 直播服务监听 [%s]:%d' % (self.host, self.port),
              flush=True)
        while not self.stop_evt.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(10.0)
            q = queue.Queue(self.queue_len)
            with self.lock:
                self.clients[conn] = q
                n = len(self.clients)
                params = self.param_sets
            print('[live] 新客户端 %s（当前 %d 路）' % (addr[0], n),
                  flush=True)
            threading.Thread(target=self._serve, args=(conn, q, params),
                             daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass

    def _serve(self, conn, q, params):
        frames = 0
        t0 = time.perf_counter()
        try:
            if params:
                conn.sendall(params)      # 先给参数集，解码器即可初始化
            hdr_sent = False
            waiting = True               # 等第一个 IDR，避免起播花屏
            while not self.stop_evt.is_set():
                try:
                    item = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                data, pts_ns = item
                if waiting:
                    if not has_idr(data):
                        continue
                    waiting = False
                if not hdr_sent:
                    # SLIVE1 + 该 AU 的 PTS(ms) + 服务端单调钟(秒, double)
                    # 后 8 字节让客户端把 PTS 直接对到本机时间轴：录制轴就是
                    # monotonic，两端只差一个常数，不必按字节码率外推，断流/
                    # 重连也不会让时间码跳动。
                    conn.sendall(b'SLIVE1' + struct.pack('<Qd',
                                pts_ns // 1000000, time.monotonic()))
                    hdr_sent = True
                conn.sendall(data)
                frames += 1
        except (OSError, socket.timeout):
            pass
        finally:
            with self.lock:
                self.clients.pop(conn, None)
                left = len(self.clients)
            try:
                conn.close()
            except OSError:
                pass
            dt = time.perf_counter() - t0
            print('[live] 客户端断开（剩 %d 路），推送 %d 帧 / %.1fs'
                  % (left, frames, dt), flush=True)



class RKEncoder:
    """RK3588 FFmpeg MPP encoder; NUT preserves access-unit boundaries.

    Input timestamps are kept in FIFO order, so camera monotonic PTS survive
    rawvideo's fixed-rate transport. No frames are intentionally discarded.
    """
    def __init__(self, w, h, bitrate, gop, callback, log_path):
        self.pts = queue.Queue(240)
        self.error = None
        self.latencies = deque(maxlen=600)
        self.count = 0
        self.callback = callback
        self.log = open(log_path, 'wb')
        cmd = ['/usr/local/bin/ffmpeg', '-hide_banner', '-loglevel', 'warning',
               '-f', 'rawvideo', '-pixel_format', 'nv12', '-video_size', '%dx%d' % (w,h),
               '-framerate', '60', '-i', 'pipe:0', '-an', '-c:v', 'hevc_rkmpp',
               '-rc_mode', 'CBR', '-b:v', str(bitrate), '-g', str(gop),
               '-flush_packets', '1', '-f', 'nut', 'pipe:1']
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, bufsize=0)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            with av.open(self.proc.stdout, format='nut', mode='r', buffer_size=4096,
                         options={'probesize': '32', 'analyzeduration': '0'}) as stream:
                extra = stream.streams.video[0].codec_context.extradata or b''
                params = b''.join(scan_nals(extra, (32,33,34)))
                if not params:
                    raise RuntimeError('HEVC encoder supplied no parameter sets')
                for packet in stream.demux(video=0):
                    if not packet.size:
                        continue
                    pts, input_ns = self.pts.get(timeout=5)
                    self.latencies.append((time.monotonic_ns() - input_ns) / 1e6)
                    data = bytes(packet)
                    if has_irap(data):
                        data = params + data
                    self.callback(data, pts)
                    self.count += 1
        except BaseException as e:
            self.error = e

    def push(self, raw, pts):
        if self.error is not None:
            raise RuntimeError('FFmpeg output failed: %r' % self.error)
        self.pts.put((pts, time.monotonic_ns()), timeout=5)
        view = memoryview(raw).cast('B')
        while view:
            n = self.proc.stdin.write(view)
            if not n:
                raise RuntimeError('FFmpeg input closed')
            view = view[n:]

    def finish(self):
        self.proc.stdin.close()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError('FFmpeg did not drain')
        rc = self.proc.wait(timeout=5)
        if self.error is not None or rc or not self.pts.empty():
            raise RuntimeError('FFmpeg failed rc=%s error=%r pending=%d' %
                               (rc, self.error, self.pts.qsize()))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()
        self.log.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=60.0,
                    help='录制时长秒（0=无限，直到收到 SIGTERM）')
    ap.add_argument('--segment', type=float, default=30.0,
                    help='分段时长秒（按关键帧对齐，默认 30）')
    ap.add_argument('--bitrate', type=int, default=30000000)
    ap.add_argument('--iframe', type=int, default=60, help='关键帧间隔帧数')
    ap.add_argument('--ring', type=float, default=90.0,
                    help='内存环形缓冲秒数（0=关闭回放服务）')
    ap.add_argument('--ring-max-mb', type=float, default=800.0)
    ap.add_argument('--port', type=int, default=9100, help='回放端口')
    ap.add_argument('--live-port', type=int, default=9101,
                    help='直播端口（0=关闭直播）')
    # 默认 0：RK3588 上两路 mpph265enc 抢 MPP 硬件，主链路从 59fps 掉到
    # 43.8fps（2026-11-18 实测）。缩略流默认关闭，监看台直连主码流 9101。
    # 真要开的话显式给 --thumb-port 9102。
    ap.add_argument('--thumb-port', type=int, default=0,
                    help='缩略直播端口（0=关闭缩略流，RK3588 默认）')
    ap.add_argument('--thumb-width', type=int, default=960,
                    help='缩略流宽度（高度按原比例）')
    ap.add_argument('--thumb-bitrate', type=int, default=2000000,
                    help='缩略流码率 bps')
    ap.add_argument('--min-free-gb', type=float, default=10.0,
                    help='磁盘剩余低于该值时优雅停止（0=不检查）')
    ap.add_argument('--out', default=os.path.expanduser('~/hk_record'))
    ap.add_argument('--reset-camera', action='store_true', help='Reset USB camera and wait for USB3 readiness before capture')
    args = ap.parse_args()
    if args.thumb_port:
        ap.error('RK3588 FFmpeg path currently requires --thumb-port 0')

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    out_dir = os.path.join(
        args.out, time.strftime('stage1_%Y%m%d_%H%M%S'))
    os.makedirs(out_dir, exist_ok=True)

    if args.min_free_gb > 0:
        free_gb = shutil.disk_usage(out_dir).free / 1e9
        if free_gb < args.min_free_gb:
            print('磁盘剩余 %.1f GB，低于 --min-free-gb %.1f，拒绝开始'
                  % (free_gb, args.min_free_gb))
            return 2
    pid_path = os.path.join(out_dir, 'recorder.pid')
    with open(pid_path, 'w') as f:
        f.write('%d\n' % os.getpid())

    cam = None
    grabbed = False
    encoder = None
    writer = SegmentWriter(out_dir, args.segment)
    ring = (RingBuffer(args.ring, args.ring_max_mb)
            if args.ring > 0 else None)
    server = None
    live = None
    thumb = None
    unlimited = args.seconds <= 0
    timings = deque(maxlen=6000)
    _cvt_dbg = [0.0, 0.0]  # [0]=预处理耗时 [1]=预处理之外(拷贝/gray视图)
    exposures = deque(maxlen=6000)
    try:
        if args.reset_camera:
            reset_usb_camera()
        cam, payload, tick = open_camera()
        print('相机: BayerGR8 payload=%.2fMB tick=%d' % (payload / 1e6, tick))

        src = np.empty(payload, np.uint8)

        # 维度和格式必须在开流前确定，但 **RK3588 上不能靠"先开一次流拿尺寸
        # 再 Stop→Start"**：那条 Jetson 上的老经验（Stop 后等 250ms 再 Start）
        # 在这里是致命的。实测（2026-11-18，3 次复现）：
        #   StartGrabbing → 取 1 帧 → StopGrabbing → sleep(0.25)
        #   → StartGrabbing → 取帧：100% 返回 0x80000007 (NODATA)，
        #   十几秒后内核报 "xhci-hcd: xHCI host not responding to stop
        #   endpoint command" / "HC died"，相机整个从 USB 总线消失，
        #   只能 `unbind/bind fc400000.usb` 重绑 dwc3 才能救回。
        # 而完全不预热（一次 StartGrabbing 直接进主循环）稳定 59.3fps 零失败。
        # 取证排除过的因素：Gst.init 时机、AE/曝光参数、mpph265enc 是否先
        # 进 PLAYING（那个另外调成 StartGrabbing 在前了，见下方注释）。
        # 所以这里直接用相机原生尺寸算，不做预热握手。
        buf = (C.c_ubyte * payload)()
        info = MV_FRAME_OUT_INFO_EX()
        w, h = 2448, 2048          # MV-CS050-60UC 原生分辨率
        print('分辨率 %dx%d' % (w, h))

        # 主链路：全分辨率编码 → 落盘。缩略链路只用于 DGX 监看：
        # 四条 1224x1024@60 直推会把 DGX 解码压垮，缩到 960 宽、2 Mbps。
        #
        # RK3588 与 Jetson 的差异：
        #  - 编码器是 mpph265enc（Rockchip MPP），参数名不同：bps 而非 bitrate，
        #    rc-mode=cbr 对应 control-rate=1，gop 对应 iframeinterval，
        #    header-mode=each-idr 等价于 insert-sps-pps=1（每 IDR 带 VPS/SPS/PPS）。
        #  - 不需要 nvvidconv：OpenCV 已经把 I420 准备好了，直接进编码器。
        #    编码器内部会把 I420 转成它要的格式（YUV420P → NV12）无需额外转换。
        #  - 缩略流在 CPU 侧resize后再编码，所以 tee 分出两条独立 appsrc 缓冲
        #    不划算；这里缩略流也走同一份 I420 缩放结果，用独立 appsrc 推送。
        # thumb_port=0 时整个缩略链路不建（RK3588 默认）：
        # 少了第二路 mpph265enc，MPP 不再被抢，主链路恢复 59fps。
        thumb_w = (args.thumb_width
                   if args.thumb_width > 0 and args.thumb_port > 0 else 0)
        thumb_h = int(round(h * thumb_w / float(w))) if thumb_w else 0
        if thumb_w and thumb_w % 2:
            thumb_w -= 1            # H.264/265 要求偶数宽
        thumb_h -= thumb_h % 2

        enc_q = queue.Queue(240)
        consume_latencies = deque(maxlen=600)
        def output(data, pts):
            enc_q.put((data, pts, time.monotonic_ns()), timeout=5)
        def consume():
            while True:
                item = enc_q.get()
                if item is None:
                    return
                data, pts, enqueued_ns = item
                consume_latencies.append((time.monotonic_ns()-enqueued_ns)/1e6)
                try:
                    irap = has_irap(data)
                    writer.write(data, pts, irap, wall_ns=t_base_wall + pts)
                    if ring is not None:
                        ring.append(data, pts, irap)
                    if live is not None:
                        live.push(data, pts)
                except BaseException as e:
                    encoder.error = e
                    return
        encoder = RKEncoder(w,h,args.bitrate,args.iframe,output,
                            os.path.join(out_dir,'ffmpeg.log'))
        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        rc = cam.MV_CC_StartGrabbing()
        if rc:
            raise RuntimeError('StartGrabbing failed %#x' % (rc & 0xffffffff))
        grabbed = True
        print('[rk] FFmpeg hevc_rkmpp / NV12 / CBR %d bps; no fake pre-roll' % args.bitrate, flush=True)
        if ring is not None:
            server = ReplayServer(ring, args.port)
            server.start()
        if args.live_port > 0:
            live = LiveServer(args.live_port)
            live.start()
        if thumb_w and args.thumb_port > 0:
            thumb = LiveServer(args.thumb_port)
            thumb.start()
        if unlimited:
            print('管线已启动，连续录制（无限时长，分段 %.0f 秒）→ %s'
                  % (args.segment, out_dir))
        else:
            print('管线已启动，录制 %.0f 秒（分段 %.0f 秒）→ %s'
                  % (args.seconds, args.segment, out_dir))
        if ring is not None:
            print('环形缓冲 %.0f 秒（上限 %.0f MB），回放端口 %d'
                  % (args.ring, args.ring_max_mb, args.port))
        if live is not None:
            print('直播端口 %d，磁盘低位阈值 %.1f GB'
                  % (args.live_port, args.min_free_gb))
        if thumb is not None:
            print('缩略直播端口 %d（%dx%d, %.1f Mbps）'
                  % (args.thumb_port, thumb_w, thumb_h,
                     args.thumb_bitrate / 1e6))


        t_base = time.monotonic_ns()
        # 绝对时间锚点：t_base 时刻对应的 UTC 纳秒。两个钟起点不同，
        # 必须同一瞬间配对取，否则会差出一大截（旧实现读开机时写的
        # /dev/shm/hk_t0_ns 再和本次启动的 monotonic 相加，误差可达
        # 数十分钟，曾导致标记时间整体偏移约 10 分钟）。
        t_base_wall = time.time_ns()
        t0 = time.perf_counter()
        sent = 0
        timeouts = 0
        consec_fail = 0          # 连续取帧失败次数（相机假死检测）
        enc_drop = 0             # 编码队列满被顶掉的帧（block=false 背压）
        alarm_path = os.path.join(out_dir, 'camera_alarm.json')
        last_frame_no = None
        cam_gaps = 0
        last_print = t0
        ref_saved = False
        stop_now = False
        disk_warned = False

        while not _stop and not stop_now and (
                unlimited or time.perf_counter() - t0 < args.seconds):
            t_a = time.perf_counter()
            ret = cam.MV_CC_GetOneFrameTimeout(C.byref(buf), payload, info, 1000)
            t_b2 = time.perf_counter()
            if ret != 0:
                if timeouts < 10:
                    print('  [diag] 取帧失败 #%d rc=%#x 耗时 %.0fms'
                          % (timeouts + 1, ret & 0xFFFFFFFF, (t_b2 - t_a) * 1e3),
                          flush=True)
                timeouts += 1
                consec_fail += 1
                # 连续 30 次（约 30 秒）取不到帧：判定相机假死/掉线，写告警文件
                # 供监看台读取；不自动复位 USB——实测软件复位会让相机更糟，须物理拔插。
                if consec_fail == 5:
                    try:
                        with open(alarm_path, 'w') as af:
                            json.dump({'ts': time.time(),
                                       'local': time.strftime('%F %T'),
                                       'seconds_stuck': consec_fail,
                                       'last_ret': int(ret & 0xFFFFFFFF),
                                       'timeouts': timeouts}, af)
                    except OSError:
                        pass
                if consec_fail >= 5:
                    raise RuntimeError('Camera returned no data for 5 consecutive seconds')
                continue
            if consec_fail:
                consec_fail = 0
                if os.path.exists(alarm_path):
                    try:
                        os.remove(alarm_path)
                    except OSError:
                        pass
            now_ns = time.monotonic_ns()
            if info.fExposureTime > 0:
                exposures.append(info.fExposureTime)
            if last_frame_no is not None:
                d = (info.nFrameNum - last_frame_no) & 0xFFFFFFFF
                if d > 1:
                    cam_gaps += d - 1
            last_frame_no = info.nFrameNum

            # 用 frombuffer 直接零拷贝取相机缓冲视图，省掉一次 5MB 的
            # ndarray 赋值（实测这一步在热循环里约 1.5~2ms）
            src_view = np.frombuffer(buf, np.uint8, info.nFrameLen)

            # 预处理：Bayer → BGR → I420，全在 OpenCV(CPU) 上做。
            # OpenCV 的 BGR2YUV_I420 输出的就是 limited range(16-235)，
            # 所以**不要**再做 Jetson 版那步 NPP 拉伸，否则二次压缩发灰。
            gray = src_view[:w * h].reshape(h, w)
            _cv_t = time.perf_counter()
            bgr, i420 = preprocess(gray)
            t_c = time.perf_counter()
            _cvt_dbg[0] = (t_c - _cv_t) * 1e3
            _cvt_dbg[1] = (_cv_t - t_b2) * 1e3    # 取帧后到预处理前的开销

            # 参考帧要跳过开机头 30 帧：AE 从接近全黑开始收敛
            # （实测帧0=22.7 帧1=38.1 帧2=57.6 帧3=63.4，均值逐帧爬升），
            # 存第 0 帧会得到一张几乎全黑的图，误导判断。
            if not ref_saved and sent >= 30:
                cv2.imwrite('/tmp/pipeline_ref.jpg', bgr)
                ref_saved = True

            pts = now_ns - t_base
            # 复用静态普通 buffer（详见 memguard_buffer 的注释：既躲开
            # 编码器不释放内存的泄漏，又避开 GstVideoBufferPool 的慢路径）。
            # 试过 GstVideoBufferPool：内存不涨但编码只有 5~20fps，已否决。
            flat = i420.reshape(-1)
            pixels = w * h
            nv12 = np.empty_like(flat)
            nv12[:pixels] = flat[:pixels]
            nv12[pixels::2] = flat[pixels:pixels + pixels // 4]
            nv12[pixels + 1::2] = flat[pixels + pixels // 4:]
            encoder.push(nv12, pts)
            sent += 1
            t_d = time.perf_counter()
            timings.append(((t_b2 - t_a) * 1000.0,
                            (t_c - t_b2) * 1000.0,
                            (t_d - t_c) * 1000.0))
            if os.environ.get('HK_DEBUG'):
                rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                print('[dbg] 等帧 %.1f 拷贝 %.1f 预处理 %.1f 入队 %.1f ms'
                      % ((t_b2 - t_a) * 1e3, _cvt_dbg[1], _cvt_dbg[0],
                         (t_d - t_c) * 1e3), flush=True)

            now = time.perf_counter()
            if now - last_print >= 2.0:
                dt = now - t0
                r_span, r_mb = ring.info() if ring is not None else (0, 0)
                print('  %.1fs 取 %d 帧 %.1f FPS, 编码 %d 帧, %.1f MB (%.1f Mbps), 丢帧(相机) %d, 编码队列 %d, 超时 %d, 环形 %.1fs/%.0fMB'
                      % (dt, sent, sent / dt, writer.total_frames,
                         writer.total_bytes / 1e6,
                         writer.total_bytes * 8 / dt / 1e6, cam_gaps,
                         enc_drop, timeouts, r_span, r_mb))
                print('[lat] pending=%d enc_q=%d input_to_AU_p50/p99_ms=%s consume_p50/p99_ms=%s' %
                      (encoder.pts.qsize(), enc_q.qsize(),
                       np.percentile(list(encoder.latencies),[50,99]).round(1).tolist() if encoder.latencies else [],
                       np.percentile(list(consume_latencies),[50,99]).round(1).tolist() if consume_latencies else []), flush=True)
                if args.min_free_gb > 0:
                    free_gb = shutil.disk_usage(out_dir).free / 1e9
                    if free_gb < args.min_free_gb:
                        print('\n磁盘剩余 %.1f GB < %.1f GB，优雅停止录制'
                              % (free_gb, args.min_free_gb))
                        stop_now = True
                    elif free_gb < args.min_free_gb * 2 and not disk_warned:
                        print('  磁盘提醒：剩余 %.1f GB' % free_gb)
                        disk_warned = True
                last_print = now

        dt = time.perf_counter() - t0
        print('\n录制结束: 取 %d 帧 / %.2f 秒 = %.1f FPS，相机丢帧 %d，'
              '编码输入丢帧 %d，超时 %d'
              % (sent, dt, sent / dt, cam_gaps, enc_drop, timeouts))

        encoder.finish()
        enc_q.put(None, timeout=5)
        consumer.join(timeout=10)
        if consumer.is_alive() or encoder.error is not None:
            raise RuntimeError('Recording consumer failed: %r' % encoder.error)
        end_pts = time.monotonic_ns() - t_base
        writer.close(end_pts)
        if writer.total_frames != sent:
            raise RuntimeError('Frame count mismatch: captured=%d encoded=%d' % (sent,writer.total_frames))
        enc_frames = writer.total_frames
        print('编码 %d 帧 = %.1f FPS，%d 个关键帧，总 %.1f MB = %.2f Mbps'
              % (enc_frames, enc_frames / dt, writer.keyframes,
                 writer.total_bytes / 1e6, writer.total_bytes * 8 / dt / 1e6))
        print('分段:')
        for rec in writer.segments:
            print('  %-28s %5d 帧  %7.1f MB  %6.1f s'
                  % (rec['file'], rec['frames'], rec['bytes'] / 1e6,
                     (rec['end_ms'] - rec['start_ms']) / 1e3))
        if ring is not None:
            print('环形缓冲: 最终 %.1fs / %.0f MB，处理回放请求 %d 次'
                  % (ring.info()[0], ring.info()[1], ring.fetches))
        if timings:
            arr = np.array(timings)
            for col, name in enumerate(('等帧', 'CPU预处理', '编码输入')):
                v = arr[:, col]
                print('%s: 均值 %.2f ms  p50 %.2f  p99 %.2f  max %.2f'
                      % (name, v.mean(), np.percentile(v, 50),
                         np.percentile(v, 99), v.max()))
            tot = arr.sum(axis=1)
            print('每帧合计: 均值 %.2f ms  p99 %.2f（60fps 预算 16.67）'
                  % (tot.mean(), np.percentile(tot, 99)))
        if exposures:
            e = np.array(exposures)
            print('曝光时间: 均值 %.0f us  min %.0f  max %.0f'
                  % (e.mean(), e.min(), e.max()))
        with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
            json.dump({'seconds': dt, 'captured': sent,
                       'unlimited': unlimited,
                       'encoded_frames': enc_frames,
                       'encoded_bytes': writer.total_bytes,
                       'camera_gaps': cam_gaps, 'timeouts': timeouts,
                       'segments': writer.segments}, f, indent=1)
        print('完成，输出目录 %s' % out_dir)
        return 0
    except KeyboardInterrupt:
        print('收到中断')
        return 130
    except Exception as e:
        print('错误: %s: %s' % (type(e).__name__, e))
        return 3
    finally:
        if encoder is not None:
            encoder.stop()
        if server is not None:
            server.stop()
        if live is not None:
            live.stop()
        if thumb is not None:
            thumb.stop()
        try:
            writer.close()
        except Exception:
            pass
        try:
            os.remove(pid_path)
        except OSError:
            pass
        if cam is not None:
            try:
                if grabbed:
                    cam.MV_CC_StopGrabbing()
                cam.MV_CC_CloseDevice()
                cam.MV_CC_DestroyHandle()
            except BaseException as e:
                print('清理异常(忽略):', e)
        print('已安全退出')


if __name__ == '__main__':
    sys.exit(main())
