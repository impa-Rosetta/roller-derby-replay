#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机端：显示 Jetson 相机 JPEG 推流的实时窗口（调焦用）

用法:
  /usr/bin/python3 hk_stream_view.py [--host 192.168.123.2] [--port 9000]
按键: q / ESC 退出
"""
import argparse
import socket
import struct
import sys
import time

import cv2
import numpy as np

WIN = 'Jetson camera stream  [q]uit'


def recv_all(s, n):
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def connect_addr(host, port):
    if host.count('%') == 1:
        addr, zone = host.split('%')
        return (addr, port, 0, socket.if_nametoindex(zone))
    return (host, port, 0, 0)


def main():
    ap = argparse.ArgumentParser()
    # 直连时是 IPv6 link-local（fe80::...%enP7s7），接交换机后改用固定 IPv4。
    # 现场 IP 若不同，用 --host 覆盖即可。
    ap.add_argument('--host', default='192.168.123.2')
    ap.add_argument('--port', type=int, default=9000)
    args = ap.parse_args()

    s = None
    for attempt in range(20):
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(connect_addr(args.host, args.port))
            break
        except OSError as e:
            s.close()
            s = None
            print('连接失败(%s)，2s 后重试 %d/20 ...' % (e, attempt + 1))
            time.sleep(2)
    if s is None:
        print('无法连接 Jetson 推流服务')
        return 1

    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(10.0)
    print('已连接，开始预览（q/ESC 退出）')

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    n = 0
    shown = 0
    fps = 0.0
    t0 = time.perf_counter()
    t_prev = t0
    try:
        while True:
            hdr = recv_all(s, 4)
            if hdr is None:
                print('推流服务关闭了连接')
                break
            size = struct.unpack('<I', hdr)[0]
            data = recv_all(s, size)
            if data is None:
                print('接收中断')
                break
            img = cv2.imdecode(np.frombuffer(data, np.uint8),
                               cv2.IMREAD_COLOR)
            if img is None:
                continue
            n += 1
            shown += 1
            now = time.perf_counter()
            if now - t_prev >= 0.5:
                fps = shown / (now - t_prev)
                shown = 0
                t_prev = now
            cv2.putText(img, 'Jetson %.1f FPS  %dx%d' % (fps, img.shape[1], img.shape[0]),
                        (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow(WIN, img)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            try:
                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
    except socket.timeout:
        print('接收超时，退出')
    finally:
        s.close()
        cv2.destroyAllWindows()
        total = time.perf_counter() - t0
        if n:
            print('结束: %d 帧 / %.1fs = %.1f FPS' % (n, total, n / total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
