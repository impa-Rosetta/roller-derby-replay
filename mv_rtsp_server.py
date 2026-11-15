#!/usr/bin/env python3
"""相机 RTSP 流服务器（gst-rtsp-server）——供海康 NVR「自定义协议」拉流录像

MVSDK 相机 → appsrc → x264enc(H.264 main) → rtph264pay → 挂载 /cam，
TCP/UDP 均可，零 root（8554 端口）。NVR 自定义协议填:
  rtsp://192.168.123.254:8554/cam

默认 AE 自动曝光（监控流随环境光自适应）+ 色卡校准+观感去绿数字增益(80,87,122)；
--exp-us 30000 可切固定曝光（帧率≤1000/exp_us/1000）。
--test 用 videotestsrc 合成源（不占相机，先验证 NVR 链路用）。

用法:
  # 链路测试（合成源）
  /usr/bin/python3 /home/ifit/xjh/mv_rtsp_server.py --test
  # 正式相机流
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD:/home/ifit/xjh LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 \
    /usr/bin/python3 /home/ifit/xjh/mv_rtsp_server.py
"""
import argparse
import signal
import sys
import threading
import time

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib  # noqa: E402

CAL_DGAIN = (80, 87, 122)     # 色卡校准(80,97,122)+观感去绿 G-10%（2026-10-17）
AE_TARGET = 100

CAM_LAUNCH = (
    "( appsrc name=cam is-live=true block=true format=time ! "
    "queue max-size-buffers=3 leaky=downstream ! "
    "videoconvert ! video/x-raw,format=I420 ! "
    "x264enc tune=zerolatency speed-preset=ultrafast "
    "bitrate={bitrate} key-int-max={kf} ! "
    "rtph264pay name=pay0 pt=96 )"
)
TEST_LAUNCH = (
    "( videotestsrc is-live=true pattern=ball ! "
    "video/x-raw,width=1280,height=720,framerate={fps}/1 ! "
    "timeoverlay ! videoconvert ! "
    "x264enc tune=zerolatency speed-preset=ultrafast "
    "bitrate=2000 key-int-max=50 ! "
    "rtph264pay name=pay0 pt=96 )"
)


class Server:
    def __init__(self, args):
        self.args = args
        self.cam = None
        self.w = self.h = 0
        self.feeders = []          # [(stop_evt, thread)]

    # ---------- 相机 ----------
    def start_camera(self):
        import cv2  # noqa: F401  (确保 OpenCV 可用，冗余但直观)
        import mvsdk
        from mv_camera_test import Camera
        self.cam = Camera()
        print("相机:", self.cam.dev.GetFriendlyName(),
              f"{self.cam.w}x{self.cam.h}", flush=True)
        self.cam.play()
        self.w, self.h = self.cam.w, self.cam.h
        mvsdk.CameraSetGain(self.cam.hcam, *CAL_DGAIN)
        mvsdk.CameraSetGamma(self.cam.hcam, 100)
        if self.args.exp_us > 0:
            self.cam.set_ae(False)
            self.cam.set_exposure(self.args.exp_us)
            self.cam.set_gain(16)
            print(f"固定曝光 {self.args.exp_us / 1000:.1f}ms + 16x", flush=True)
        else:
            self.cam.set_ae(True, AE_TARGET)
            print(f"AE 自动曝光（目标 {AE_TARGET}）", flush=True)
        for _ in range(10):        # 参数生效/收敛预热
            self.cam.grab()

    # ---------- 喂帧线程 ----------
    def feed_loop(self, appsrc, stop_evt):
        fps = self.args.fps
        period = 1.0 / fps
        idx = 0
        t0 = time.time()
        n = 0
        while not stop_evt.is_set():
            frame = self.cam.grab()
            target = t0 + idx * period
            dt = target - time.time()
            if dt > 0:
                time.sleep(dt)
            elif dt < -2.0:        # 落后太多，重置节奏
                t0, idx = time.time(), 0
            buf = Gst.Buffer.new_allocate(None, frame.nbytes, None)
            buf.fill(0, frame.tobytes())
            buf.pts = buf.dts = int(idx * Gst.SECOND / fps)
            buf.duration = int(Gst.SECOND / fps)
            if appsrc.emit('push-buffer', buf) != Gst.FlowReturn.OK:
                break
            idx += 1
            n += 1
            if n % (fps * 30) == 0:
                print(f"  已推送 {n} 帧（{n / (time.time() - t0):.1f} FPS）",
                      flush=True)
        print(f"喂帧线程结束（共 {n} 帧）", flush=True)

    def on_media_configure(self, factory, media):
        if self.args.test:
            return
        pipe = media.get_element()
        appsrc = pipe.get_by_name('cam')
        if appsrc is None:
            print("警告: 管线里找不到 appsrc", flush=True)
            return
        caps = Gst.Caps.from_string(
            f"video/x-raw,format=BGR,width={self.w},height={self.h},"
            f"framerate={self.args.fps}/1")
        appsrc.set_property('caps', caps)
        stop_evt = threading.Event()
        th = threading.Thread(target=self.feed_loop, args=(appsrc, stop_evt),
                              daemon=True)
        th.start()
        self.feeders.append((stop_evt, th))
        print(f"客户端接入 → 开始喂帧（{self.w}x{self.h}@{self.args.fps}）",
              flush=True)

        def on_unprepared(_m):
            stop_evt.set()
            print("客户端断开 → 停止喂帧", flush=True)
        media.connect('unprepared', on_unprepared)

    def stop(self):
        for stop_evt, th in self.feeders:
            stop_evt.set()
            th.join(timeout=3)
        if self.cam is not None:
            try:
                self.cam.close()
            except BaseException as e:
                print("相机清理异常(忽略):", e, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true',
                    help='用 videotestsrc 合成源（不占相机）')
    ap.add_argument('--port', type=int, default=8554)
    ap.add_argument('--fps', type=int, default=25)
    ap.add_argument('--bitrate', type=int, default=6000, help='kbps（默认 6000）')
    ap.add_argument('--exp-us', type=int, default=0,
                    help='固定曝光微秒（默认 0 = AE 自动曝光）')
    args = ap.parse_args()

    Gst.init(None)
    srv = Server(args)

    server = GstRtspServer.RTSPServer()
    server.set_service(str(args.port))
    factory = GstRtspServer.RTSPMediaFactory()
    if args.test:
        factory.set_launch(TEST_LAUNCH.format(fps=args.fps))
    else:
        factory.set_launch(CAM_LAUNCH.format(bitrate=args.bitrate,
                                             kf=args.fps * 2))
    factory.set_shared(True)
    # 挂载 "/"：匹配任意路径（NVR Custom 协议路径未知，万能兜底；
    # /cam 请求同样命中）。标准 554 端口需先放开非特权端口：
    #   sudo sysctl net.ipv4.ip_unprivileged_port_start=554
    server.get_mount_points().add_factory("/", factory)

    if not args.test:
        try:
            srv.start_camera()
        except Exception as e:
            print(f"相机初始化失败: {type(e).__name__}: {e}", flush=True)
            return 3

    factory.connect('media-configure', srv.on_media_configure)
    server.attach(None)
    print(f"RTSP 服务器就绪: rtsp://<本机IP>:{args.port}/cam  "
          f"（NVR 填 rtsp://192.168.123.254:{args.port}/cam）", flush=True)

    loop = GLib.MainLoop()

    def on_signal(_sig, _frm):
        print("\n收到停止信号，收尾中...", flush=True)
        loop.quit()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    code = 0
    try:
        loop.run()
    except KeyboardInterrupt:
        code = 130
    finally:
        srv.stop()
        print("已安全退出", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
