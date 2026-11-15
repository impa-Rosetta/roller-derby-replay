#!/usr/bin/env python3
"""模拟 RK3588 边缘采集节点（在 DGX Spark 上验证）—— 轮滑阻拦赛判罚项目

职责模拟（3588 最终形态）：
  相机取流(MVSDK) → 压缩编码 → 带时间戳/帧号协议 → 网络上行

本版编码为 JPEG（cv2，实测吞吐 >164fps）；3588 上替换为 mpp H.265 硬编码，
传输 v2 可演进 RTP/RTSP。协议头设计为可扩展（magic 校验 + 时间戳 + 帧号）。

帧协议（自定义，大端）:
  magic "MVED"(4B) | 时间戳 float64(8B, Unix 秒) | 帧号 uint32(4B) | 数据长 uint32(4B) | JPEG 数据

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_edge_node.py [--host 127.0.0.1] [--port 9020] [--duration 10] [--quality 85]
"""
import argparse
import signal
import socket
import struct
import sys
import time

import cv2
from mv_camera_test import Camera

HDR = struct.Struct(">4sdII")
MAGIC = b"MVED"
FIXED_EXPOSURE_US = 6000
FIXED_GAIN = 16
JPEG_QUALITY = 85


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="中央节点地址")
    ap.add_argument("--port", type=int, default=9020)
    ap.add_argument("--duration", type=float, default=10.0,
                    help="推流时长秒（0=一直推）")
    ap.add_argument("--quality", type=int, default=JPEG_QUALITY)
    ap.add_argument("--gain", type=int, default=FIXED_GAIN)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    sock = None
    exit_code = 0
    try:
        cam = Camera()
        print(f"边缘节点: {cam.dev.GetFriendlyName()} {cam.w}x{cam.h}")
        cam.play()
        cam.set_ae(False)
        cam.set_exposure(FIXED_EXPOSURE_US)
        cam.set_gain(args.gain)
        for _ in range(5):  # 参数生效
            cam.grab()

        print(f"连接中央节点 {args.host}:{args.port} ...")
        sock = socket.create_connection((args.host, args.port), timeout=5)
        sock.settimeout(None)
        print("已连接，开始推流")

        t0 = time.time()
        last = t0
        n = 0
        total = 0
        while args.duration <= 0 or time.time() - t0 < args.duration:
            frame = cam.grab()
            ok, buf = cv2.imencode(
                '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
            if not ok:
                continue
            data = buf.tobytes()
            sock.sendall(
                HDR.pack(MAGIC, time.time(), n & 0xFFFFFFFF, len(data)) + data)
            n += 1
            total += len(data)
            now = time.time()
            if now - last >= 2.0:
                d = now - t0
                print(f"  {d:5.1f}s  已发 {n} 帧 ({n / d:.1f} FPS)  "
                      f"{total * 8 / d / 1e6:.1f} Mbps")
                last = now
        dur = time.time() - t0
        if n:
            print(f"推流结束: {n} 帧 / {dur:.2f}s = {n / dur:.1f} FPS, "
                  f"平均 {total * 8 / dur / 1e6:.1f} Mbps, "
                  f"均帧 {total / n / 1024:.1f} KB")
    except KeyboardInterrupt:
        print("\n收到中断信号")
        exit_code = 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}")
        exit_code = 3
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if cam is not None:
            try:
                cam.close()
            except BaseException as e:
                print("关闭清理异常(忽略):", e)
        print("已安全退出")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
