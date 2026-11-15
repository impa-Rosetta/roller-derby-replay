#!/usr/bin/env python3
"""MindVision SUA202GC 视频录制（MVSDK）

录制固定时长（默认 10s，6ms+16x+色卡校准增益，无窗口全速取流）。
流程：全速取流 + 逐帧 JPEG 编码缓存到内存 → 录完按**实测帧率**写出
MJPG-AVI，回放速度与真实时间一致。防弹框架复用 mv_camera_test.Camera
（取帧超时重试 / finally 释放 / 优雅关闭：q 无窗口，用 Ctrl+C 或 SIGTERM 停）。

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_camera_record.py [--seconds 10] [--exp-us 6000] [--out 路径.avi]
"""
import argparse
import os
import signal
import sys
import time

import cv2
import mvsdk
from mv_camera_test import Camera

FIXED_EXPOSURE_US = 6000
FIXED_GAIN = 16
CAL_DGAIN = (80, 87, 122)  # 色卡校准(80,97,122)+观感去绿 G-10%（2026-10-17）
GAMMA = 100
JPEG_QUALITY = 85
SAVE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_FRAMES = 12000  # 内存保险丝默认值（60s@165fps≈9900帧）；--max-frames 可调


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def save_video(jpegs, duration, out, width, height):
    """把缓存的 JPEG 帧按实测帧率写成 MJPG-AVI，返回 fps"""
    fps = len(jpegs) / duration if duration > 0 else 25.0
    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*'MJPG'),
                             fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"无法创建视频文件: {out}")
    try:
        for i, buf in enumerate(jpegs):
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is not None:
                writer.write(img)
            if (i + 1) % 300 == 0:
                print(f"  写盘 {i + 1}/{len(jpegs)} ...")
    finally:
        writer.release()
    return fps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="录制时长秒（默认 10）")
    ap.add_argument("--exp-us", type=int, default=FIXED_EXPOSURE_US,
                    help=f"曝光微秒（默认 {FIXED_EXPOSURE_US}=满帧率；"
                         "光照不足时可调大降帧率）")
    ap.add_argument("--dgain", default=",".join(map(str, CAL_DGAIN)),
                    help="ISP 数字增益 R,G,B（默认色卡校准值；"
                         "暗环境提亮演示可试 333,400,316）")
    ap.add_argument("--gamma", type=int, default=GAMMA,
                    help="ISP gamma（默认 100；40=大幅提亮、噪声同步放大）")
    ap.add_argument("--denoise", action="store_true",
                    help="开启 ISP 降噪（噪声滤波 + 3D时域降噪4帧默认权重）")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                    help=f"内存保险丝帧数上限（默认 {MAX_FRAMES}）")
    ap.add_argument("--out", default="",
                    help="输出 .avi 路径（默认 脚本目录/record_时间戳.avi）")
    args = ap.parse_args()

    out = args.out or os.path.join(
        SAVE_DIR, time.strftime("record_%Y%m%d_%H%M%S.avi"))

    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    jpegs = []
    t0 = None
    exit_code = 0
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), f"{cam.w}x{cam.h}")
        cam.play()
        cam.set_ae(False)
        cam.set_exposure(args.exp_us)
        cam.set_gain(FIXED_GAIN)
        dg = [int(v) for v in args.dgain.split(",")]
        if len(dg) != 3:
            raise ValueError("--dgain 需要 R,G,B 三个整数")
        mvsdk.CameraSetGain(cam.hcam, *dg)
        mvsdk.CameraSetGamma(cam.hcam, args.gamma)
        warmup = 5
        if args.denoise:
            mvsdk.CameraSetNoiseFilter(cam.hcam, 1)
            mvsdk.CameraSetDenoise3DParams(cam.hcam, 1, 4, None)
            print("ISP 降噪: NoiseFilter + Denoise3D(4帧默认权重)", flush=True)
            warmup = 60  # 3D 降噪历史帧积累（约 0.4s）
        for _ in range(warmup):  # 参数生效
            cam.grab()

        t0 = time.time()
        last = t0
        print(f"开始录制 {args.seconds}s（{args.exp_us / 1000:.1f}ms+"
              f"{FIXED_GAIN}x 全速，无窗口）...")
        while True:
            now = time.time()
            if now - t0 >= args.seconds or len(jpegs) >= args.max_frames:
                break
            frame = cam.grab()
            ok, buf = cv2.imencode('.jpg', frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                jpegs.append(buf)
            if now - last >= 2.0:
                print(f"  {now - t0:.1f}s  已缓存 {len(jpegs)} 帧 "
                      f"({len(jpegs) / (now - t0):.1f} FPS)")
                last = now
        print(f"录制结束: {len(jpegs)} 帧 / {time.time() - t0:.2f}s"
              f"（{len(jpegs) / (time.time() - t0):.1f} FPS）")
    except KeyboardInterrupt:
        print("\n收到中断信号，保存已录制部分...")
        exit_code = 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}")
        exit_code = 3
    finally:
        dur = (time.time() - t0) if t0 else 0.0
        if cam is not None:
            try:
                cam.close()
            except BaseException as e:
                print("关闭清理异常(忽略):", e)
        if jpegs:
            try:
                fps = save_video(jpegs, dur, out, cam.w, cam.h)
                print(f"已存: {out}  ({len(jpegs)} 帧 @ {fps:.1f} FPS, "
                      f"{os.path.getsize(out) / 1e6:.1f} MB)")
            except BaseException as e:
                print("写视频失败:", e)
                exit_code = 4
        print("已安全退出")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
