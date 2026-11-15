#!/usr/bin/env python3
"""裁判手持端 MJPEG 推流服务

从 Jetson 9101 拉 H.265 直播 → NVDEC 硬件解码 → JPEG 编码 → HTTP multipart 推流。
供 ESP32-P4 手持屏（硬件 JPEG 解码）或普通浏览器直接观看。

用法:
    python3 handheld_server.py --http-port 8088 --width 1024 --fps 25
    # 浏览器打开 http://<DGX IP>:8088/live.mjpg 即可看到直播
"""
import argparse
import queue
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import cv2

sys.path.insert(0, '/home/ifit/xjh')
import dgx_viewer as v  # noqa: E402

BOUNDARY = b'--frame'


class FrameHub:
    """单路 NVDEC 解码，JPEG 限速编码，最新帧广播给所有 HTTP 客户端"""

    STALL_TIMEOUT = 6.0     # 已有帧后，超过该秒数无新帧即判定断流并重连
    WARMUP_TIMEOUT = 20.0   # 首帧宽限（NVDEC 管线初始化约需 10s）
    RETRY_INTERVAL = 2.0    # 重连失败后的等待间隔

    def __init__(self, host, port, width, quality, max_fps,
                 height=0, rotate=0):
        self.host = host
        self.port = port
        self.stop_evt = threading.Event()
        self.q = queue.Queue(1)          # LiveDecoder 满则丢旧，只保留最新
        self.dec = None                  # 由 _supervise 线程建立/重建
        self.sock = None
        self.dec_stop = threading.Event()
        self.width = width
        self.quality = quality
        self.height = height             # >0 时强制缩放到 width×height
        self.rotate = rotate             # 0/90/180/270，顺时针
        self.interval = 1.0 / max_fps
        self.latest = None
        self.seq = 0
        self.cv = threading.Condition()
        self.next_enc = 0.0         # 截止时间累加，避免与源帧间隔量化导致掉速
        self.frames_in = 0
        self.frames_out = 0
        self._reported = False

    def start(self):
        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._encode_loop, daemon=True).start()

    def _teardown(self):
        """停掉当前解码线程并关闭上游 socket"""
        self.dec_stop.set()
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
        self.dec_stop = threading.Event()
        self.dec = None

    def _supervise(self):
        """维护到 Jetson 的上游连接：断开或无帧超时后自动重连"""
        while not self.stop_evt.is_set():
            try:
                sock = v.connect(self.host, self.port, timeout=5.0)
            except OSError as e:
                print('连接上游失败: %s，%.0fs 后重试'
                      % (e, self.RETRY_INTERVAL), flush=True)
                self.stop_evt.wait(self.RETRY_INTERVAL)
                continue
            self.sock = sock
            dec = v.LiveDecoder(sock, self.q, self.dec_stop, self.width)
            self.dec = dec
            dec.start()
            print('已连接上游 %s:%d' % (self.host, self.port), flush=True)

            last_frames = dec.frames
            last_change = time.time()
            while not self.stop_evt.is_set():
                self.stop_evt.wait(1.0)
                if not dec.is_alive():
                    break
                if dec.frames != last_frames:
                    last_frames = dec.frames
                    last_change = time.time()
                    continue
                timeout = (self.WARMUP_TIMEOUT if last_frames == 0
                           else self.STALL_TIMEOUT)
                if time.time() - last_change > timeout:
                    print('上游 %.0fs 无新帧，重连' % timeout, flush=True)
                    break
            if not self.stop_evt.is_set():
                print('上游断开，重连中…', flush=True)
            self._teardown()
            self.stop_evt.wait(self.RETRY_INTERVAL)

    def _encode_loop(self):
        while not self.stop_evt.is_set():
            try:
                img = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.frames_in += 1
            now = time.time()
            if now < self.next_enc:
                continue                 # 限速：多出来的帧直接丢
            self.next_enc += self.interval
            if self.next_enc < now:      # 编码落后时重新对齐，不补帧
                self.next_enc = now + self.interval
            if self.height > 0:
                img = cv2.resize(img, (self.width, self.height))
            if self.rotate == 90:
                img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            elif self.rotate == 180:
                img = cv2.rotate(img, cv2.ROTATE_180)
            elif self.rotate == 270:
                img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            ok, buf = cv2.imencode('.jpg', img,
                                   [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            if not ok:
                continue
            if not self._reported:
                self._reported = True
                print('解码器: %s' % (getattr(self.dec, 'decoder', None) or '?'),
                      flush=True)
            self.frames_out += 1
            with self.cv:
                self.latest = buf.tobytes()
                self.seq += 1
                self.cv.notify_all()

    def wait_new(self, last_seq, timeout=5.0):
        """阻塞到出现比 last_seq 新的帧；超时返回 None"""
        with self.cv:
            if not self.cv.wait_for(lambda: self.seq != last_seq, timeout):
                return None, last_seq
            return self.latest, self.seq

    def close(self):
        self.stop_evt.set()
        self._teardown()


class Handler(BaseHTTPRequestHandler):
    hub = None

    def do_GET(self):
        if self.path.startswith('/live.mjpg'):
            self._serve_mjpeg()
        elif self.path.startswith('/snapshot.jpg'):
            self._serve_snapshot()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_snapshot(self):
        data, _ = self.hub.wait_new(-1, timeout=5.0)
        if data is None:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_mjpeg(self):
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        last = -1
        while True:
            data, last = self.hub.wait_new(last, timeout=5.0)
            if data is None:
                break                    # 5 秒没新帧（断流），断开客户端
            try:
                hdr = (BOUNDARY + b'\r\nContent-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(data)).encode() +
                       b'\r\n\r\n')
                self.wfile.write(hdr)
                self.wfile.write(data)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break

    def log_message(self, fmt, *args):
        pass                             # 静默，避免每帧刷屏


def main():
    ap = argparse.ArgumentParser()
    # 直连时是 IPv6 link-local（fe80::...%enP7s7），接交换机后改用固定 IPv4。
    # 现场 IP 若不同，用 --host-jetson 覆盖即可。
    ap.add_argument('--host-jetson', default='192.168.123.2')
    ap.add_argument('--live-port', type=int, default=9101)
    ap.add_argument('--http-port', type=int, default=8088)
    ap.add_argument('--width', type=int, default=1024)
    ap.add_argument('--height', type=int, default=0,
                    help='0 表示按宽度保持原始比例')
    ap.add_argument('--rotate', type=int, default=0,
                    choices=(0, 90, 180, 270), help='顺时针旋转角度')
    ap.add_argument('--fps', type=float, default=25.0)
    ap.add_argument('--quality', type=int, default=70)
    args = ap.parse_args()

    hub = FrameHub(args.host_jetson, args.live_port,
                   args.width, args.quality, args.fps,
                   args.height, args.rotate)
    hub.start()
    Handler.hub = hub

    srv = ThreadingHTTPServer(('0.0.0.0', args.http_port), Handler)
    srv.daemon_threads = True
    print('MJPEG 服务已启动: http://0.0.0.0:%d/live.mjpg'
          % args.http_port, flush=True)
    try:
        srv.serve_forever()
    finally:
        hub.close()


if __name__ == '__main__':
    main()
