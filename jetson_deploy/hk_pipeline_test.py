#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段1：单相机 60fps 实时管线（NPP 去马赛克 + NVENC 硬编 + 分段落盘）

数据流：
  相机 BayerGR8 2448x2048@60
    → NPP(GPU) CFA→RGB（相位 GRBG）→ RGB→I420（全范围）
    → NPP 拉伸到 limited range（Y*219/255+16，UV*224/255+16）
    → appsrc → nvvidconv(VIC) → NV12 NVMM → nvv4l2h265enc
    → appsink 按关键帧切分裸流 .h265（每段自带 VPS/SPS/PPS）

裸流分段是为阶段 2 的环形缓冲做准备：编码后的 AU 在内存中保留最近 N 秒，
裁判随时可以取最近一段回放，磁盘写入不中断。

用法（Jetson）:
  source /etc/profile.d/mvs_env.sh
  /usr/bin/python3 hk_pipeline_test.py --seconds 60 --segment 30 --out ~/hk_record

按键/信号：SIGINT/SIGTERM 优雅停止
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
from collections import deque

import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

import cv2  # noqa: E402
import gi  # noqa: E402

gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

CUDA_LIB = '/usr/local/cuda/lib64'
GST_FMT = Gst.Format.TIME
FRAME_NS = int(1e9 / 60)
GRID = 3          # NPP GRBG，与相机 BayerGR8 对应（实测相位差 0.40）
OPAQUE = 0        # NPPI_INTER_UNDEFINED

_stop = False


def _on_term(_sig, _frame):
    global _stop
    _stop = True


class NppiSize(C.Structure):
    _fields_ = [('width', C.c_int), ('height', C.c_int)]


class NppiRect(C.Structure):
    _fields_ = [('x', C.c_int), ('y', C.c_int),
                ('width', C.c_int), ('height', C.c_int)]


def load_libs():
    rt = C.CDLL(os.path.join(CUDA_LIB, 'libcudart.so.12'))
    npp = C.CDLL(os.path.join(CUDA_LIB, 'libnppicc.so.12'))
    nppal = C.CDLL(os.path.join(CUDA_LIB, 'libnppial.so.12'))

    npp.nppiCFAToRGB_8u_C1C3R.argtypes = [
        C.c_void_p, C.c_int, NppiSize, NppiRect,
        C.c_void_p, C.c_int, C.c_int, C.c_int]
    npp.nppiCFAToRGB_8u_C1C3R.restype = C.c_int
    npp.nppiRGBToYUV420_8u_C3P3R.argtypes = [
        C.c_void_p, C.c_int, C.POINTER(C.c_void_p),
        C.POINTER(C.c_int), NppiSize]
    npp.nppiRGBToYUV420_8u_C3P3R.restype = C.c_int
    for fn in (nppal.nppiMulC_8u_C1IRSfs, nppal.nppiAddC_8u_C1IRSfs):
        fn.argtypes = [C.c_ubyte, C.c_void_p, C.c_int, NppiSize, C.c_int]
        fn.restype = C.c_int

    rt.cudaHostRegister.argtypes = [C.c_void_p, C.c_size_t, C.c_uint]
    rt.cudaHostGetDevicePointer.argtypes = [
        C.POINTER(C.c_void_p), C.c_void_p, C.c_uint]
    rt.cudaDeviceSynchronize.restype = C.c_int
    rt.cudaHostUnregister.argtypes = [C.c_void_p]
    return rt, npp, nppal


def register(rt, arr):
    addr = arr.ctypes.data
    rc = rt.cudaHostRegister(C.c_void_p(addr), arr.nbytes, 2)
    if rc != 0:
        raise RuntimeError('cudaHostRegister rc=%d (%.1fMB)'
                           % (rc, arr.nbytes / 1e6))
    p = C.c_void_p()
    rc = rt.cudaHostGetDevicePointer(C.byref(p), C.c_void_p(addr), 0)
    if rc != 0:
        raise RuntimeError('cudaHostGetDevicePointer rc=%d' % rc)
    return p.value or addr


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
    ap.add_argument('--thumb-port', type=int, default=9102,
                    help='缩略直播端口（0=关闭缩略流）')
    ap.add_argument('--thumb-width', type=int, default=960,
                    help='缩略流宽度（高度按原比例）')
    ap.add_argument('--thumb-bitrate', type=int, default=2000000,
                    help='缩略流码率 bps')
    ap.add_argument('--min-free-gb', type=float, default=10.0,
                    help='磁盘剩余低于该值时优雅停止（0=不检查）')
    ap.add_argument('--out', default=os.path.expanduser('~/hk_record'))
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    Gst.init(None)

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

    rt, npp, nppal = load_libs()
    cam = None
    grabbed = False
    pipeline = None
    writer = SegmentWriter(out_dir, args.segment)
    ring = (RingBuffer(args.ring, args.ring_max_mb)
            if args.ring > 0 else None)
    server = None
    live = None
    thumb = None
    unlimited = args.seconds <= 0
    timings = []
    exposures = []
    try:
        cam, payload, tick = open_camera()
        print('相机: BayerGR8 payload=%.2fMB tick=%d' % (payload / 1e6, tick))

        src = np.empty(payload, np.uint8)
        p_src = register(rt, src)

        # 维度和格式必须在开流前确定：先取一帧拿尺寸，再建管线。
        # 拿到尺寸就立刻退出循环 —— 实测冷启动首轮 StartGrabbing 只出
        # 约 2 帧就进入 NODATA 窗口（0x80000007），后续每帧要空等满 1 秒
        # 超时才返回；旧代码固定取满 5 帧，等于白卡 3 秒，还会让首帧帧号
        # 错位。这不是相机假死，第二轮起就稳定 58.8fps。
        buf = (C.c_ubyte * payload)()
        info = MV_FRAME_OUT_INFO_EX()
        cam.MV_CC_StartGrabbing()
        grabbed = True
        w = h = 0
        warm_fail = 0
        for _ in range(8):
            rc = cam.MV_CC_GetOneFrameTimeout(C.byref(buf), payload, info, 1000)
            if rc == 0:
                w, h = info.nWidth, info.nHeight
                break
            warm_fail += 1
            if rc != 0x80000007:
                print('预热取帧异常 rc=%#x' % (rc & 0xFFFFFFFF))
        if not w:
            raise RuntimeError('取帧失败（预热 %d 次全部超时）' % warm_fail)
        if warm_fail:
            print('预热: 失败 %d 次后拿到首帧（冷启动 NODATA 窗口）' % warm_fail)
        cam.MV_CC_StopGrabbing()
        grabbed = False
        # Stop 后必须让相机喘口气才能再 Start：实测无间隔立刻 StartGrabbing
        # 会永久卡在 NODATA（0x80000007，2.4fps），隔 10ms 就恢复满载
        # 59.2fps。留 250ms 富余，代价可以忽略。
        time.sleep(0.25)
        print('分辨率 %dx%d' % (w, h))

        rgb = np.empty((h, w, 3), np.uint8)
        p_rgb = register(rt, rgb)
        i420 = np.empty(w * h * 3 // 2, np.uint8)
        p_i420 = register(rt, i420)
        off_y, off_u, off_v = 0, w * h, w * h + w * h // 4
        p_u = p_i420 + off_u
        p_v = p_i420 + off_v
        dst3 = (C.c_void_p * 3)(p_i420, p_u, p_v)
        step3 = (C.c_int * 3)(w, w // 2, w // 2)
        size = NppiSize(w, h)
        size_uv = NppiSize(w // 2, h // 2)
        rect = NppiRect(0, 0, w, h)

        # 主链路：全分辨率编码 → 落盘。缩略链路从同一个 tee 分出，
        # 只用于 DGX 监看：四条 1224x1024@60 直推会把 DGX 解码压垮，
        # 缩到 960 宽、2 Mbps 后四路合计约 8 Mbps。
        # 两路编码器是独立的硬编单元（实测 tee 双路 30 帧 0.47s 跑通）。
        thumb_w = args.thumb_width if args.thumb_width > 0 else 0
        thumb_h = int(round(h * thumb_w / float(w))) if thumb_w else 0
        if thumb_w and thumb_w % 2:
            thumb_w -= 1            # H.264/265 要求偶数宽
        thumb_h -= thumb_h % 2
        if thumb_w and thumb_h:
            pipeline = Gst.parse_launch(
                'appsrc name=src is-live=true format=time do-timestamp=false '
                'max-buffers=8 block=true '
                '! video/x-raw,format=I420,width=%d,height=%d,framerate=60/1 '
                '! tee name=t '
                't. ! queue max-size-buffers=8 leaky=downstream '
                '  ! nvvidconv '
                '  ! video/x-raw(memory:NVMM),format=NV12 '
                '  ! nvv4l2h265enc name=enc bitrate=%d control-rate=1 '
                '    preset-level=1 maxperf-enable=1 iframeinterval=%d '
                '    insert-sps-pps=1 insert-aud=1 '
                '  ! appsink name=sink sync=false max-buffers=8 '
                't. ! queue max-size-buffers=4 leaky=downstream '
                '  ! nvvidconv '
                '  ! video/x-raw(memory:NVMM),width=%d,height=%d,format=NV12 '
                '  ! nvv4l2h265enc name=tenc bitrate=%d control-rate=1 '
                '    preset-level=1 maxperf-enable=1 iframeinterval=%d '
                '    insert-sps-pps=1 insert-aud=1 '
                '  ! appsink name=tsink sync=false max-buffers=4'
                % (w, h, args.bitrate, args.iframe,
                   thumb_w, thumb_h, args.thumb_bitrate, args.iframe))
        else:
            pipeline = Gst.parse_launch(
                'appsrc name=src is-live=true format=time do-timestamp=false '
                'max-buffers=8 block=true '
                '! video/x-raw,format=I420,width=%d,height=%d,framerate=60/1 '
                '! nvvidconv '
                '! video/x-raw(memory:NVMM),format=NV12 '
                '! nvv4l2h265enc name=enc bitrate=%d control-rate=1 '
                'preset-level=1 maxperf-enable=1 iframeinterval=%d '
                'insert-sps-pps=1 insert-aud=1 '
                '! appsink name=sink sync=false max-buffers=8'
                % (w, h, args.bitrate, args.iframe))
        appsrc = pipeline.get_by_name('src')
        appsink = pipeline.get_by_name('sink')
        tsink = pipeline.get_by_name('tsink')

        def _on_sample(sink):
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.ERROR
            gbuf = sample.get_buffer()
            ok, mapinfo = gbuf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.ERROR
            data = bytes(mapinfo.data)
            gbuf.unmap(mapinfo)
            pts = gbuf.pts if gbuf.pts != Gst.CLOCK_TIME_NONE else 0
            is_irap = has_irap(data)
            writer.write(data, pts, is_irap,
                         wall_ns=t_base_wall + pts)
            if ring is not None:
                ring.append(data, pts, is_irap)
            if live is not None:
                live.push(data, pts)
            return Gst.FlowReturn.OK

        appsink.set_property('emit-signals', True)
        appsink.connect('new-sample', _on_sample)

        # 缩略流回调：只推给 DGX 监看，不进环形缓冲、不落盘。
        # PTS 用主链路的时钟（同一次采集），监看端据此对齐回放位置。
        def _on_thumb(sink):
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.ERROR
            gbuf = sample.get_buffer()
            ok, mapinfo = gbuf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.ERROR
            data = bytes(mapinfo.data)
            gbuf.unmap(mapinfo)
            pts = gbuf.pts if gbuf.pts != Gst.CLOCK_TIME_NONE else 0
            if thumb is not None:
                thumb.push(data, pts)
            return Gst.FlowReturn.OK

        if tsink is not None and args.thumb_port > 0:
            tsink.set_property('emit-signals', True)
            tsink.connect('new-sample', _on_thumb)

        pipeline.set_state(Gst.State.PLAYING)
        if ring is not None:
            server = ReplayServer(ring, args.port)
            server.start()
        if args.live_port > 0:
            live = LiveServer(args.live_port)
            live.start()
        if thumb_w and args.thumb_port > 0:
            thumb = LiveServer(args.thumb_port)
            thumb.start()
        cam.MV_CC_StartGrabbing()
        grabbed = True
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

        bus = pipeline.get_bus()
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
            t_b = time.perf_counter()
            if ret != 0:
                if timeouts < 10:
                    print('  [diag] 取帧失败 #%d rc=%#x 耗时 %.0fms'
                          % (timeouts + 1, ret & 0xFFFFFFFF, (t_b - t_a) * 1e3),
                          flush=True)
                timeouts += 1
                consec_fail += 1
                # 连续 30 次（约 30 秒）取不到帧：判定相机假死/掉线，写告警文件
                # 供监看台读取；不自动复位 USB——实测软件复位会让相机更糟，须物理拔插。
                if consec_fail == 30:
                    try:
                        with open(alarm_path, 'w') as af:
                            json.dump({'ts': time.time(),
                                       'local': time.strftime('%F %T'),
                                       'seconds_stuck': consec_fail,
                                       'last_ret': int(ret & 0xFFFFFFFF),
                                       'timeouts': timeouts}, af)
                    except OSError:
                        pass
                continue
            if consec_fail:
                consec_fail = 0
                if os.path.exists(alarm_path):
                    try:
                        os.remove(alarm_path)
                    except OSError:
                        pass
            now_ns = time.monotonic_ns()
            exposures.append(info.fExposureTime)
            if last_frame_no is not None:
                d = (info.nFrameNum - last_frame_no) & 0xFFFFFFFF
                if d > 1:
                    cam_gaps += d - 1
            last_frame_no = info.nFrameNum

            src[:info.nFrameLen] = np.frombuffer(
                buf, np.uint8, info.nFrameLen)

            # 全范围→limited：Y=Y*219/255+16，UV=(UV*224/255)+16
            # NPP 的乘数是 8bit，取 220/256、225/256，误差 <0.1%
            rc = npp.nppiCFAToRGB_8u_C1C3R(
                p_src, w, size, rect, p_rgb, w * 3, GRID, OPAQUE)
            rc |= npp.nppiRGBToYUV420_8u_C3P3R(
                p_rgb, w * 3, dst3, step3, size)
            rc |= nppal.nppiMulC_8u_C1IRSfs(220, p_i420, w, size, 8)
            rc |= nppal.nppiAddC_8u_C1IRSfs(16, p_i420, w, size, 0)
            rc |= nppal.nppiMulC_8u_C1IRSfs(225, p_u, w // 2, size_uv, 8)
            rc |= nppal.nppiAddC_8u_C1IRSfs(16, p_u, w // 2, size_uv, 0)
            rc |= nppal.nppiMulC_8u_C1IRSfs(225, p_v, w // 2, size_uv, 8)
            rc |= nppal.nppiAddC_8u_C1IRSfs(16, p_v, w // 2, size_uv, 0)
            if rc != 0:
                raise RuntimeError('NPP rc=%d' % rc)
            rt.cudaDeviceSynchronize()
            t_c = time.perf_counter()

            if not ref_saved:
                cv2.imwrite('/tmp/pipeline_ref.jpg',
                            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                ref_saved = True

            gbuf = Gst.Buffer.new_wrapped(i420.tobytes())
            pts = now_ns - t_base
            gbuf.pts = pts
            gbuf.dts = pts
            gbuf.duration = FRAME_NS
            if appsrc.emit('push-buffer', gbuf) != Gst.FlowReturn.OK:
                raise RuntimeError('appsrc push 失败')
            sent += 1
            t_d = time.perf_counter()
            timings.append(((t_b - t_a) * 1000.0, (t_c - t_b) * 1000.0,
                            (t_d - t_c) * 1000.0))

            msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
            if msg is not None:
                err, dbg = msg.parse_error()
                raise RuntimeError('GStreamer: %s (%s)' % (err.message, dbg))

            now = time.perf_counter()
            if now - last_print >= 2.0:
                dt = now - t0
                r_span, r_mb = ring.info() if ring is not None else (0, 0)
                print('  %.1fs 取 %d 帧 %.1f FPS, 编码 %d 帧, %.1f MB (%.1f Mbps), 丢帧(相机) %d, 超时 %d, 环形 %.1fs/%.0fMB'
                      % (dt, sent, sent / dt, writer.total_frames,
                         writer.total_bytes / 1e6,
                         writer.total_bytes * 8 / dt / 1e6, cam_gaps,
                         timeouts, r_span, r_mb))
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
        print('\n录制结束: 取 %d 帧 / %.2f 秒 = %.1f FPS，相机丢帧 %d，超时 %d'
              % (sent, dt, sent / dt, cam_gaps, timeouts))

        appsrc.emit('end-of-stream')
        msg = bus.timed_pop_filtered(15 * Gst.SECOND,
                                     Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is not None and msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print('编码尾段错误: %s (%s)' % (err.message, dbg))
        else:
            print('编码器已收尾（EOS）')

        end_pts = time.monotonic_ns() - t_base
        writer.close(end_pts)
        pipeline.set_state(Gst.State.NULL)
        pipeline = None

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
            for col, name in enumerate(('等帧', 'NPP+同步', '入队')):
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
        if server is not None:
            server.stop()
        if live is not None:
            live.stop()
        if thumb is not None:
            thumb.stop()
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
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
