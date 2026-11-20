#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DGX 回放台：实时监看 + 最近片段回放（慢放/逐帧）

用法:
  /usr/bin/python3 dgx_viewer.py                 # 打开直播窗口
  /usr/bin/python3 dgx_viewer.py --speed 0.5 --replay-seconds 20
  /usr/bin/python3 dgx_viewer.py --headless --decode-seconds 10

按键:
  q / ESC   退出            s         截图
  r         拉取最近 N 秒    l         回到直播
  space     暂停/继续        ← / →     逐帧
  1 2 3 4   0.25x/0.5x/1x/2x
"""
import argparse
import os
import queue
import socket
import struct
import sys
import threading
import time

import numpy as np
import cv2

import av

from hk_replay_fetch import HDR, connect_addr, recv_all, remux_mkv

WIN = 'DGX Replay'
AUD = 35          # NAL type: access unit delimiter
SRC_ASPECT = 2048.0 / 2448.0   # MV-CS050-60UC 传感器 2448×2048

_gst_ok = None


def gst_live_elements():
    """挑出可用的 (解码器, 缩放器)；没有硬解组合时返回 None

    两个平台各有一套，都是硬件解码 + 硬件缩放：
      DGX/Jetson  nvh265dec   + videoscale   （实测约 250 fps）
      RK3588      mppvideodec + rgaconvert   （实测 530 fps 纯解，含 RGA
                                             缩放后 170 fps）
    缩放必须走硬件：RK3588 上 mppvideodec + 软件 videoscale 只有 5.6 fps
    （2026-11-19 实测，480 帧 30 秒才解 168 帧），换 rgaconvert 后 170 fps，
    差了 30 倍。四路直播同开时这个差距是能不能用的分水岭。
    """
    global _gst_ok
    if _gst_ok is not None:
        return _gst_ok or None
    _gst_ok = None
    try:
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstApp', '1.0')
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)
        have = Gst.ElementFactory.find
        for dec, scale in (('nvh265dec', 'videoscale'),
                           ('mppvideodec', 'rgaconvert')):
            if have(dec) and have(scale) and have('h265parse') \
                    and have('appsrc') and have('appsink'):
                _gst_ok = (dec, scale)
                break
    except Exception:
        _gst_ok = None
    return _gst_ok


def gst_live_available():
    """GStreamer 硬解直播是否可用"""
    return gst_live_elements() is not None


def split_aus(buf):
    """切出完整 AU，末尾不完整的留在 buf 里

    以前只认 AUD(NAL 35) 作为 AU 边界，但 RK3588 的 mpph265enc 不产出
    AUD（实测直播流里 NAL 类型只有 32/33/34/19/1，一个 35 都没有），
    结果一个 AU 都切不出来，直播永久 0 帧。

    现在按"每个 VCL 帧各自成一个 AU"切：

      VPS(32)/SPS(33)/PPS(34) 是参数集，跟在所属 AU 的头部；
      VCL(0..31，即 TRAIL_R/IDR/CRA 等) 每出现一个就结束当前 AU。
      因为编码器是 gop=N 无 B 帧（首帧 IDR，其余 P），一帧正好一个 VCL
      NAL，所以一个 AU = 参数集(可选) + 一个 VCL。

    末尾的半个 AU（最后一个 VCL 后面还没收到数据）留着等下一批——
    切早了解码器会拿到截断的帧。
    """
    ends = []           # 每个完整 AU 的结束位置（下一个 AU 的起始）
    next_start = 0      # 当前 AU 的起始
    seen_vcl = False
    i = 0
    while True:
        j = buf.find(b'\x00\x00\x01', i)
        if j < 0 or j + 3 >= len(buf):
            break
        t = (buf[j + 3] >> 1) & 0x3F
        if t < 32:                      # VCL NAL
            if seen_vcl:                # 上一帧已经完整，这里是新 AU 的开始
                ends.append((next_start, j))
                next_start = j
            seen_vcl = True
        i = j + 3
    if not ends:
        return [], buf
    out = [buf[a:b] for a, b in ends]
    return out, buf[next_start:]


def au_has_idr(au):
    """AU 里是否含 IDR（HEVC NAL type 19/20），积压后可从它开始续解"""
    i = 0
    while True:
        j = au.find(b'\x00\x00\x01', i)
        if j < 0 or j + 3 >= len(au):
            return False
        t = (au[j + 3] >> 1) & 0x3F
        if t in (19, 20):
            return True
        i = j + 3


def strip_live_header(buf):
    """剥掉服务端插在参数集之后的 'SLIVE1' + 16 字节头。

    返回 (剩下的字节, 是否已剥过)。头没到齐就原样返回，等下一批。
    软解路径以前完全不剥这个头，头里的 21 个字节直接喂给 PyAV，
    hevc 解析器碰到就报错、一帧都解不出来（2026-11-19 在 RK3588 上
    实测 frames=0，而服务端明明在推 2.5 MB/s）。硬解路径有
    LiveDecoder._consume_header 负责，软解这条路也必须剥。
    """
    i = buf.find(b'SLIVE1')
    if i < 0:
        return buf, False
    if len(buf) < i + 22:       # 6 字节标记 + 8 PTS + 8 monotonic
        return buf, False
    tail = buf[i + 22:i + 26]
    if not (tail[:3] == b'\x00\x00\x01' or tail[:4] == b'\x00\x00\x00\x01'
            or (len(tail) < 4 and tail == b'\x00\x00\x00\x01'[:len(tail)])):
        # 头后面不是起始码：可能还是解析没对齐，保守起见多等一批
        if len(buf) < i + 32:
            return buf, False
    return buf[:i] + buf[i + 22:], True


def decode_stream(sock, on_frame, stop_evt, on_first=None, target_width=0):
    """从裸 HEVC 字节流解码；返回 (帧数, 字节数)

    target_width > 0 时由 PyAV 在解码后直接缩放，避免「全尺寸 BGR +
    cv2.resize」的开销（实测 60.6 fps，后者仅 52.0 fps；低于 Jetson
    约 59 fps 的产出速度会让服务端队列积压、直播延迟越拉越大）。
    """
    dec = av.CodecContext.create('hevc', 'r')
    try:
        dec.thread_count = 0
    except Exception:
        pass
    buf = b''
    hdr_done = False
    frames = nbytes = 0
    while not stop_evt.is_set():
        data = sock.recv(1 << 16)
        if not data:
            break
        nbytes += len(data)
        buf += data
        if not hdr_done:
            buf, hdr_done = strip_live_header(buf)
        aus, buf = split_aus(buf)
        for au in aus:
            for frame in dec.decode(av.Packet(au)):
                if target_width and frame.width != target_width:
                    h = int(round(frame.height * target_width / frame.width))
                    img = frame.reformat(width=target_width, height=h,
                                         format='bgr24').to_ndarray()
                else:
                    img = frame.to_ndarray(format='bgr24')
                if frames == 0 and on_first is not None:
                    on_first()
                frames += 1
                if not on_frame(img, len(au)):
                    return frames, nbytes
    return frames, nbytes


class LiveDecoder(threading.Thread):
    """后台解码直播流，只保留最新帧（队列满则丢旧帧以降低延迟）

    优先走 GStreamer NVDEC 硬解（DGX 实测约 250 fps）：吞吐远高于
    Jetson 约 59 fps 的产出，服务端 240 帧缓冲会被排空，直播延迟不会
    随时间累积。NVDEC 不可用时回退 PyAV 软解（约 60 fps）。
    """

    def __init__(self, sock, out_q, stop_evt, width=1280):
        super().__init__(daemon=True)
        self.sock = sock
        self.q = out_q
        self.stop_evt = stop_evt
        self.width = width
        self.frames = 0
        self.nbytes = 0
        self.first_t = None
        self.decoder = 'pyav'
        # 服务端在首个 IDR 前发的 'SLIVE1' + u64 毫秒 PTS（相对录制起点）。
        # 配合 /dev/shm 的绝对锚点即可算出真实端到端延迟；旧服务端没有此头。
        self.stream_t0 = None     # 本机收到首帧的单调时刻
        self.first_pts_ms = None
        # 本连接的录制轴：SLIVE1 头的 PTS(ms) 是首帧时刻，
        # 本机收到它的时刻 close 于首帧的"实时"时刻，两者之差就是
        # 本连接的开播偏移（含建链后的排队）。
        # pos = 头 PTS + (本连接字节增量) / 码率，比拿绝对时间钟去对更稳：
        # 服务端时钟有没有错、拨没拨，都不影响画面显示的时刻。
        self.pub_pts_ms = None    # 本连接头里的首帧 PTS(ms)
        self.pub_mono = None      # 头里附带的服务端 monotonic（仅调试用）
        self.reconnect = 0        # 连接代号：LiveFeed 用它判断换没换连接
        self._first_src_pts = None
        self._first_play = None   # 本连接首帧的录制轴秒数（对齐基准）
        self.skipped = 0          # 为追延迟丢弃的 AU 数
        # 每帧精确 PTS：服务端 appsrc 的 PTS 是累积字节数，压帧后仍单调，
        # 把"首帧 PTS ↔ 首帧累积字节"做线性换算即可还原本帧录制轴时刻。
        self.first_pts_s = None   # 首帧录制轴秒数
        self.last_pts_s = None    # 最近交付帧的录制轴秒数
        # 该帧交付时刻（monotonic）与它的录制轴 PTS。两者之差 = 这一帧
        # 从被采集到被本机解出来花了多久（传输 + 缓冲 + 解码），不含
        # "服务端自己的缓冲"，但足以判断画面跟得上、以及去抖动。
        self.last_pts_t = 0.0
        # PTS 与上次采样的"墙钟"进度之差：两端时钟各走各的，
        # 这个偏移稳定，正好用来把 PTS 搬到本机时间轴上量延迟。
        self._clock_off = None    # 本机 monotonic - PTS 的稳定偏移
        self._base_pts = 0
        self._last_src_pts = 0
        self._base_bytes = 0
        self._src_bytes = 0

    @property
    def clock_off(self):
        return self._clock_off

    @property
    def last_src_pts(self):
        """最近交付帧的 appsrc 字节 PTS（录制轴换算用）"""
        return self._last_src_pts

    @property
    def live_pos(self):
        """屏幕上这帧在录制轴上是第几秒；还没对齐返回 None。

        用本连接的字节增量算（3.7 MB/s），不依赖任何绝对时间钟。
        """
        if self._first_play is None or self.last_src_pts is None:
            return None
        return self._first_play + (self.last_src_pts
                                   - self._first_src_pts) / 3.7e6

    LIVE_HDR = b'SLIVE1'
    LAG_BYTES = 2 << 20       # appsrc 积压上限：超过即丢帧追实时

    def _consume_header(self, buf):
        """从流里剥掉 SLIVE1 头（服务端把它放在参数集之后），返回剩余字节。

        用 find 而不是只比对开头：头的位置取决于 TCP 分段。
        返回的码流从**下一个起始码**开始切：新旧协议头长不同也无关，
        新协议多出的 8 字节（服务端 monotonic）会跟着头一起丢掉，
        不会混进 ES（混进 ES 会让 h265parse 报错/花屏）。
        头没到齐时**原样返回**：头前面还有参数集，不能丢（NVDEC 要用）。
        """
        i = buf.find(self.LIVE_HDR)
        if i < 0:
            # 头没到：原样留着。不能只留尾部 5 字节——头前面还有参数集，
            # 下游 NVDEC 的 h265parse 要靠它们建解码器。
            return buf
        if len(buf) < i + 14:
            return buf              # 连 PTS 都没到齐：原样等下一批
        self.first_pts_ms = struct.unpack('<Q', buf[i + 6:i + 14])[0]
        self._base_pts = self.first_pts_ms
        self.pub_pts_ms = self.first_pts_ms
        # 新协议在 PTS 后跟 8 字节服务端 monotonic（double）；旧协议紧接码流。
        # 必须以"后面跟着起始码"为准，单用 double 范围会误判：旧协议头后
        # 就是 ES，用它自己前 8 字节解 double 也能落在范围里（实测
        # 2228876.0078125），按新协议切就会吃掉 8 字节码流（起始码没了，
        # split_aus 直接瞎）。
        # 判定需要看到 i+14+3 字节，否则起始码可能被 TCP 切成 1~2 个 0，
        # 急着剥会砍掉它的前导 0。
        if len(buf) < i + 17:
            return buf
        if self._post_hdr_ok(buf, i + 14):
            # 旧协议：起始码就在 i+14
            self.pub_mono = None
            return buf[:i] + buf[i + 14:]
        if len(buf) < i + 25:
            return buf              # 新协议起始码也可能被切，等
        cand = struct.unpack('<d', buf[i + 14:i + 22])[0]
        if 1.0 <= cand <= 1e8 and self._post_hdr_ok(buf, i + 22):
            self.pub_mono = cand
            return buf[:i] + buf[i + 22:]
        # 两边都对不上：头肯定还没切全（服务端一次 write，最多多等一包）
        return buf

    @staticmethod
    def _post_hdr_ok(buf, pos):
        """pos 处头之后、最多 3 字节内是否开始一个 H.265 起始码 00 00 01。

        返回 True 当且仅当（pos+4 之内）能看到起始码**或**数据在 pos 处
        就断了、且已见字节是起始码的前缀（全 0）。只看 8 字节窗口：
        ES 里任意位置都有起始码，窗口过大等于永远 True。
        """
        sc = b'\x00\x00\x01'
        tail = buf[pos:pos + 4]
        if tail[:3] == sc:
            return True
        if tail[:3] == b'\x00\x00\x00' and len(tail) >= 4:
            return tail[3] == 1      # 4 字节起始码：00 00 00 01
        # 不够 3 字节：已见的是起始码前缀（全 0）就先等，不是则判假
        return len(tail) < 3 and tail == sc[:len(tail)]

    def _push(self, img, pts=0):
        self.frames += 1
        if self.first_pts_ms is not None and pts > 0:
            # 本连接首帧记为字节起点：累积字节可能带上上一连接的尾巴
            if self._first_src_pts is None or pts < self._first_src_pts:
                self._first_src_pts = pts
                self._first_play = self._base_pts / 1000.0
            # appsrc 的 PTS=累积字节数；换算成字节 → 秒（15 B/帧 @ 59.2fps
            # ≈ 888 B/s 的码率×码率倒数，29.6 Mbps = 3.7 MB/s）
            ps = (self._base_pts / 1000.0
                  + (pts - self._first_src_pts) / 3.7e6)
            now = time.monotonic()
            # 两端单调钟各走各的，但相对偏移固定：单帧就能量出
            # 传输+解码 = 交付时刻 - (该帧录制轴时刻 + 时钟偏移)。
            off = now - ps
            prev = self._clock_off
            if prev is None or abs(off - prev) > 60.0:
                self._clock_off = off           # 复位/重连：直接采信
            else:
                self._clock_off = prev + 0.5 * (off - prev)
            self.last_pts_s = ps
            self.last_pts_t = now
        else:
            self.last_pts_s = None
            self.last_pts_t = 0.0
        if self.first_t is None:
            self.first_t = time.perf_counter()
            self.stream_t0 = self.first_t
        if self.q.full():
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
        try:
            self.q.put_nowait(img)
        except queue.Full:
            pass

    def _on_frame(self, img, n):
        self.nbytes += n
        if self.width and img.shape[1] != self.width:
            h = int(round(img.shape[0] * self.width / img.shape[1]))
            img = cv2.resize(img, (self.width, h),
                             interpolation=cv2.INTER_AREA)
        self._push(img)
        return True

    def _run_gst(self):
        """appsrc → NVDEC 硬解 → NV12 缩放到 width → BGR"""
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstApp', '1.0')
        from gi.repository import Gst, GstApp   # GstApp 提供 appsink 拉帧接口

        w = int(self.width)
        h = int(round(w * SRC_ASPECT)) & ~1      # NV12 要求偶数高度
        dec, scale = gst_live_elements()
        pipe = Gst.parse_launch(
            # 沿用 appsrc 的累积字节 PTS（"码率 × 字节"≈时长，压帧后仍单调），
            # 再配合首帧的绝对 PTS / 字节数换算出每帧的录制轴时刻
            'appsrc name=src is-live=true format=time do-timestamp=false '
            'block=true max-bytes=4194304 '
            'caps=video/x-h265,stream-format=byte-stream,alignment=au ! '
            'h265parse ! %s ! %s ! '
            'video/x-raw,format=NV12,width=%d,height=%d ! '
            'appsink name=sink max-buffers=1 drop=true sync=false '
            'emit-signals=false' % (dec, scale, w, h))
        src = pipe.get_by_name('src')
        sink = pipe.get_by_name('sink')
        bus = pipe.get_bus()
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipe.set_state(Gst.State.NULL)
            raise RuntimeError('%s 启动失败' % dec)
        self.decoder = 'nvdec' if dec == 'nvh265dec' else 'mpp'

        stop = self.stop_evt
        sock = self.sock

        def pusher():
            buf = b''
            hdr_done = False
            drop_gop = False
            head = None          # 首帧绝对 PTS（毫秒）在拿到后从未交付前沿用
            while not stop.is_set():
                try:
                    data = sock.recv(1 << 16)
                except OSError:
                    break
                if not data:
                    break
                self.nbytes += len(data)
                buf += data
                if not hdr_done:
                    buf = self._consume_header(buf)
                    # 判据看"头信息是否解析完"，不能看 first_pts_ms：
                    # 它一等到 14 字节就先赋上了，而新协议还剩 8 字节没到齐
                    # （提前收工会把那 8 字节当码流喂给解码器）。
                    hdr_done = self.pub_pts_ms is not None
                    if hdr_done and head is None:
                        head = self.first_pts_ms
                    self._hdr_wait = getattr(self, '_hdr_wait', 0) + 1
                    if self._hdr_wait > 40:      # 约 40 个 TCP 段仍无头＝旧服务端
                        hdr_done = True
                        print('直播流无时间头（旧版 Jetson 录制端），'
                              '延迟改按封存进度估算', flush=True)
                aus, buf = split_aus(buf)
                for au in aus:
                    # 追延迟：解码器内部积压时丢掉中间帧，只跟进最新。
                    # 否则一旦积压就永远补不回来（实测延迟会一路涨到 28s）。
                    try:
                        level = src.get_current_level_bytes()
                        if drop_gop or level > self.LAG_BYTES:
                            # 积压时丢掉整个 GOP 直到下一个 IDR：丢 P 帧会让
                            # 后面的 P 帧全解不出来（画面变花/卡住）
                            if not au_has_idr(au):
                                drop_gop = True
                                self.skipped += 1
                                continue
                            drop_gop = False
                        gb = Gst.Buffer.new_wrapped(au)
                        gb.pts = self._src_bytes
                        self._last_src_pts = self._src_bytes
                        self._src_bytes += len(au)
                        if head is not None:
                            self.first_pts_ms = head
                        src.push_buffer(gb)
                    except Exception:
                        return
            try:
                src.end_of_stream()
            except Exception:
                pass

        pt = threading.Thread(target=pusher, daemon=True)
        pt.start()
        need = w * h * 3 // 2
        try:
            while not stop.is_set():
                sample = sink.try_pull_sample(100 * 1000 * 1000)
                if sample is not None:
                    b = sample.get_buffer()
                    buf = sample.get_buffer()
                    ok, mi = buf.map(Gst.MapFlags.READ)
                    if ok:
                        try:
                            raw = np.frombuffer(mi.data, np.uint8)
                            if raw.size >= need:
                                yuv = raw[:need].reshape(h * 3 // 2, w)
                                # 用 appsrc 透传的 PTS（=累积字节数）；
                                # 解码器输出的 PTS 可能被重排成 None/0
                                self._push(cv2.cvtColor(
                                    yuv, cv2.COLOR_YUV2BGR_NV12),
                                    self._last_src_pts)
                        finally:
                            b.unmap(mi)
                msg = bus.timed_pop_filtered(
                    0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is not None:
                    if msg.type == Gst.MessageType.ERROR:
                        err, _dbg = msg.parse_error()
                        if not stop.is_set():
                            print('NVDEC 直播错误: %s' % err.message)
                    return
        finally:
            # 顺序很关键：先停推送线程，等它退出，再拆管线。
            # 反过来的话 pipe.set_state(NULL) 会等 appsrc 清空，
            # 而 pt 还在往那个 appsrc 推数据 → 互相等（实测进程
            # 退出时报 "terminate called without an active exception"
            # 然后 Aborted）。stop_evt 由 LiveDecoder.stop() 置位。
            stop.set()
            # pusher 卡在阻塞的 sock.recv 上，置 stop_evt 它看不见：
            # shutdown 让它立刻返回 b'' 从而跳出循环。
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            pt.join(timeout=1.5)
            pipe.set_state(Gst.State.NULL)

    def run(self):
        try:
            if self.width > 0 and gst_live_available():
                try:
                    self._run_gst()
                    return
                except Exception as e:
                    # 只在还没读数据时回退，否则续解同一路流会错乱
                    if self.stop_evt.is_set() or self.nbytes:
                        raise
                    print('NVDEC 启动失败，回退软解: %s: %s'
                          % (type(e).__name__, e))
            decode_stream(self.sock, self._on_frame, self.stop_evt,
                          target_width=self.width)
        except Exception as e:
            if not self.stop_evt.is_set():
                print('直播中断: %s: %s' % (type(e).__name__, e))
        finally:
            try:
                self.sock.close()
            except OSError:
                pass


def connect(host, port, timeout=10.0):
    # 按地址形态自动选协议族：带 '' 的是 IPv6（可含 %zone 作用域），
    # 否则走 IPv4。之前硬编码 AF_INET6，接交换机后改用 192.168.x.x
    # 会直接 gaierror。
    if ':' in host:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        addr = connect_addr(host, port)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        addr = (host, port)
    s.settimeout(timeout)
    s.connect(addr)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(None)
    return s


def fetch_clip(host, port, seconds, out_dir):
    """拉取最近 seconds 秒，返回 (h265 路径, 帧数, 时长秒, fps)"""
    os.makedirs(out_dir, exist_ok=True)
    s = connect(host, port)
    try:
        s.sendall(struct.pack('<I', seconds))
        raw = recv_all(s, HDR.size)
        if raw is None:
            raise RuntimeError('服务端未返回头部')
        magic, req, start_ms, end_ms, frames, nbytes = HDR.unpack(raw)
        if magic != b'HKR1':
            raise RuntimeError('协议错误 magic=%r' % magic)
        dur = (end_ms - start_ms) / 1000.0
        fps = frames / dur if dur > 0 else 59.94
        stamp = time.strftime('%Y%m%d_%H%M%S')
        path = os.path.join(out_dir, 'replay_%s_%ds.h265' % (stamp, seconds))
        got = 0
        with open(path, 'wb') as f:
            while got < nbytes:
                chunk = s.recv(min(1 << 20, nbytes - got))
                if not chunk:
                    raise RuntimeError('传输中断 %d/%d' % (got, nbytes))
                f.write(chunk)
                got += len(chunk)
        print('拉取 %ds → %.1fs %d 帧 %.1f MB（%.2f fps）'
              % (seconds, dur, frames, nbytes / 1e6, fps))
        return path, frames, dur, fps
    finally:
        s.close()


def overlay(img, lines, y0=36):
    for i, text in enumerate(lines):
        y = y0 + i * 34
        cv2.putText(img, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.85,
                    (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.85,
                    (0, 255, 0), 1, cv2.LINE_AA)
    return img


def placeholder(text, width):
    h = int(round(width * 2048 / 2448))
    img = np.full((h, width, 3), 18, np.uint8)
    cv2.putText(img, text, (30, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (0, 200, 255), 2, cv2.LINE_AA)
    return img


def snapshot(img, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime('shot_%Y%m%d_%H%M%S.jpg'))
    cv2.imwrite(path, img)
    print('截图: %s' % path)


def watch_live(args):
    """直播窗口；返回 'replay' 或 'quit'"""
    print('连接直播 [%s]:%d …' % (args.host, args.live_port))
    try:
        sock = connect(args.host, args.live_port)
    except OSError as e:
        print('直播连接失败: %s' % e)
        return 'quit'
    evt = threading.Event()
    q = queue.Queue(4)
    dec = LiveDecoder(sock, q, evt, args.display_width)
    dec.start()

    last = None
    t0 = time.perf_counter()
    frames = 0
    try:
        while True:
            try:
                img = q.get(timeout=0.02)
            except queue.Empty:
                img = None
            if img is not None:
                last = img
                frames += 1
            view = last if last is not None else placeholder(
                'LIVE: waiting for IDR…', args.display_width)
            dt = max(1e-3, time.perf_counter() - t0)
            overlay(view, [
                'LIVE  %dx%d  %.1f fps  %.0fs' % (
                    view.shape[1], view.shape[0], frames / dt, dt),
                'r replay   s shot   q quit'])
            cv2.imshow(WIN, view)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                return 'quit'
            if key == ord('s') and last is not None:
                snapshot(last, args.out_dir)
            if key == ord('r'):
                return 'replay'
    finally:
        evt.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        dec.join(timeout=1.0)


def play_replay(args):
    """拉取并播放最近片段；返回 'live' / 'quit' / 'replay'(重新拉取)"""
    try:
        src, frames, dur, fps = fetch_clip(
            args.host, args.replay_port, args.replay_seconds, args.out_dir)
    except (OSError, RuntimeError) as e:
        print('回放拉取失败: %s' % e)
        return 'live'

    mkv = src[:-5] + '.mkv'
    if not remux_mkv(src, mkv, frames, fps):
        print('转封装失败，直接播放裸流（定位可能不准）')
        mkv = src

    cap = cv2.VideoCapture(mkv)
    if not cap.isOpened():
        print('打开回放失败: %s' % mkv)
        return 'live'
    fps_play = cap.get(cv2.CAP_PROP_FPS) or fps
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or frames

    speed = args.speed
    paused = False
    idx = 0                 # 已读帧数（显示的是 idx-1）
    last = None
    ended = False
    print('回放中：space 暂停，←/→ 逐帧，1-4 变速，r 重取，l 直播')

    try:
        while True:
            t_start = time.perf_counter()
            if not paused and not ended:
                ok, frame = cap.read()
                if ok:
                    last = frame
                    idx += 1
                else:
                    ended = True
                    paused = True

            if last is None:
                last = placeholder('REPLAY: decoding…', args.display_width)
            view = last
            pos = max(0, idx - 1) / max(fps_play, 1e-3)
            overlay(view, [
                'REPLAY %.1fs  %.2fx  frame %d/%d%s' % (
                    pos, speed, max(0, idx - 1), total,
                    '  END' if ended else ('  PAUSED' if paused else '')),
                'space pause   <-/-> step   r refetch   l live   q quit'])
            cv2.imshow(WIN, view)

            if paused or ended:
                delay = 30
            else:
                target = 1.0 / (fps_play * speed)
                delay = max(1, int((target - (time.perf_counter() - t_start))
                                   * 1000))
            key = cv2.waitKey(delay) & 0xFF

            if key in (ord('q'), 27):
                return 'quit'
            if key == ord('l'):
                return 'live'
            if key == ord('r'):
                return 'replay'
            if key == ord('s'):
                snapshot(last, args.out_dir)
            if key == ord(' '):
                if ended:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    idx = 0
                    ended = False
                    paused = False
                else:
                    paused = not paused
            if key == ord('1'):
                speed = 0.25
            elif key == ord('2'):
                speed = 0.5
            elif key == ord('3'):
                speed = 1.0
            elif key == ord('4'):
                speed = 2.0
            elif key in (81, ord('a')) and idx > 0:        # ← 后退一帧
                paused = True
                ended = False
                target = max(0, idx - 2)
                cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                ok, frame = cap.read()
                if ok:
                    last = frame
                    idx = target + 1
            elif key in (83, ord('d')) and not ended:      # → 前进一帧
                paused = True
                ok, frame = cap.read()
                if ok:
                    last = frame
                    idx += 1
                else:
                    ended = True
    finally:
        cap.release()


def run_headless(args):
    """无人值守自检：走和监看台完全相同的解码路径"""
    print('连接直播 [%s]:%d …' % (args.host, args.live_port))
    sock = connect(args.host, args.live_port)
    evt = threading.Event()
    dec = LiveDecoder(sock, queue.Queue(2), evt, args.display_width)
    dec.start()
    t0 = time.perf_counter()
    timer = threading.Timer(args.decode_seconds, evt.set)
    timer.start()
    try:
        while not evt.is_set() and dec.is_alive():
            time.sleep(0.05)
    finally:
        evt.set()
        timer.cancel()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    t_end = time.perf_counter()
    dec.join(timeout=2.0)
    dt = t_end - t0
    first = dec.first_t or t0
    play = max(1e-3, t_end - first)
    if dec.first_t is not None:
        print('首帧解码 OK（连接后 %.2fs）' % (dec.first_t - t0))
    print('直播自检[%s]: %d 帧，首帧后 %.1f fps，%.1f Mbps'
          % (dec.decoder, dec.frames, dec.frames / play,
             dec.nbytes * 8 / max(dt, 1e-3) / 1e6))
    return 0 if dec.frames > 0 else 1


def main():
    ap = argparse.ArgumentParser()
    # 直连时是 IPv6 link-local（fe80::...%enP7s7），接交换机后改用固定 IPv4。
    # 现场 IP 若不同，用 --host 覆盖即可。
    ap.add_argument('--host', default='192.168.123.2')
    ap.add_argument('--live-port', type=int, default=9101)
    ap.add_argument('--replay-port', type=int, default=9100)
    ap.add_argument('--replay-seconds', type=int, default=20)
    ap.add_argument('--speed', type=float, default=0.5)
    ap.add_argument('--display-width', type=int, default=1280)
    ap.add_argument('--out-dir', default='/home/ifit/xjh/replay')
    ap.add_argument('--mode', choices=('live', 'replay'), default='live',
                    help='启动模式（默认直播）')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--decode-seconds', type=float, default=10.0)
    args = ap.parse_args()

    if args.headless:
        return run_headless(args)
    if not os.environ.get('DISPLAY'):
        print('没有 DISPLAY，无法开窗口；可用 --headless 验证直播链路')
        return 2

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    try:
        cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    mode = args.mode
    try:
        while True:
            mode = watch_live(args) if mode == 'live' else play_replay(args)
            if mode == 'quit':
                break
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == '__main__':
    sys.exit(main())
