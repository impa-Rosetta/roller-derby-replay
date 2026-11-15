#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机端：向 Jetson 录制节点拉取「最近 N 秒」回放片段

协议（TCP，IPv6 link-local）：
  请求: u32 秒数（小端）
  响应: 36 字节头 '<4sIQQIQ' = magic 'HKR1', 请求秒数, 起始ms, 结束ms, 帧数, 字节数
        随后是裸 HEVC（段首已补 VPS/SPS/PPS）

用法:
  /usr/bin/python3 hk_replay_fetch.py --seconds 60
  /usr/bin/python3 hk_replay_fetch.py --seconds 20 --host 192.168.123.2 --port 9100
"""
import argparse
import os
import socket
import struct
import sys
import time

import cv2

HDR = struct.Struct('<4sIQQIQ')


def recv_all(s, n):
    buf = b''
    while len(buf) < n:
        chunk = s.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return buf


def connect_addr(host, port):
    if host.count('%') == 1:
        addr, zone = host.split('%')
        return (addr, port, 0, socket.if_nametoindex(zone))
    return (host, port, 0, 0)


def remux_mkv(src, dst, frames, fps):
    try:
        import av
        from fractions import Fraction
    except ImportError:
        print('（未安装 PyAV，跳过 MKV 转封装）')
        return False
    inp = av.open(src, format='hevc')
    vs = inp.streams.video[0]
    out = av.open(dst, 'w', format='matroska')
    ost = out.add_stream_from_template(vs)
    ost.time_base = Fraction(1, 1000)
    step = 1000.0 / fps
    n = 0
    for pkt in inp.demux(vs):
        if pkt.size == 0 and pkt.dts is None:
            continue
        ms = int(round(n * step))
        pkt.pts = pkt.dts = ms
        pkt.duration = int(round(step))
        pkt.time_base = ost.time_base
        pkt.stream = ost
        out.mux(pkt)
        n += 1
    out.close()
    inp.close()
    return n == frames


def main():
    ap = argparse.ArgumentParser()
    # 直连时是 IPv6 link-local（fe80::...%enP7s7），接交换机后改用固定 IPv4。
    # 现场 IP 若不同，用 --host 覆盖即可。
    ap.add_argument('--host', default='192.168.123.2')
    ap.add_argument('--port', type=int, default=9100)
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--out', default='/home/ifit/xjh/replay')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    base = os.path.join(args.out, 'replay_%s_%ds' % (stamp, args.seconds))

    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(10.0)
    try:
        s.connect(connect_addr(args.host, args.port))
    except OSError as e:
        print('连接失败: %s' % e)
        return 1

    t0 = time.perf_counter()
    s.sendall(struct.pack('<I', args.seconds))
    raw = recv_all(s, HDR.size)
    if raw is None:
        print('服务端未返回头部')
        return 1
    magic, req, start_ms, end_ms, frames, nbytes = HDR.unpack(raw)
    if magic != b'HKR1':
        print('协议错误 magic=%r' % magic)
        return 1
    print('请求 %ds → 实际 %.2f 秒，%d 帧，%.1f MB（PTS %.1f–%.1f ms）'
          % (req, (end_ms - start_ms) / 1000.0, frames, nbytes / 1e6,
             start_ms, end_ms))

    path = base + '.h265'
    got = 0
    next_pct = 0
    with open(path, 'wb') as f:
        while got < nbytes:
            chunk = s.recv(min(1 << 20, nbytes - got))
            if not chunk:
                print('传输中断: %d/%d' % (got, nbytes))
                return 1
            f.write(chunk)
            got += len(chunk)
            pct = got * 100.0 / nbytes
            if pct >= next_pct:
                print('  %3.0f%%  %.1f MB' % (pct, got / 1e6), flush=True)
                next_pct = (int(pct) // 10 + 1) * 10
    s.close()
    dt = time.perf_counter() - t0
    print('已保存 %s（%.1fs，%.1f MB/s）' % (path, dt, got / dt / 1e6))

    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    n = 0
    while ok and n < 30:
        n += 1
        ok, frame = cap.read()
    cap.release()
    print('解码自检: 首帧 %s，前 %d 帧读取%s'
          % ('OK' if frame is not None else '失败', n,
             '正常' if n == 30 else '异常'))

    fps = frames / ((end_ms - start_ms) / 1000.0) if end_ms > start_ms else 59.94
    mkv = base + '.mkv'
    if remux_mkv(path, mkv, frames, fps):
        print('已转封装 %s（%.2f fps）' % (mkv, fps))
    else:
        print('MKV 转封装失败或帧数不符，可先用 %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
