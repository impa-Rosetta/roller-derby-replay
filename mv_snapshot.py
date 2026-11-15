#!/usr/bin/env python3
"""MindVision SUA202GC 快照工具（AE 收敛冻结 / 固定曝光）

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_snapshot.py --out snap.png [--dgain R,G,B] [--exp-us 6000 --gain 16]
"""
import argparse
import signal
import sys
import time

import cv2
import mvsdk
from mv_camera_test import Camera


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ae-sec", type=float, default=4.0)
    ap.add_argument("--ae-target", type=int, default=100)
    ap.add_argument("--exp-us", type=int, default=0, help="0=AE 收敛模式；>0=固定曝光")
    ap.add_argument("--gain", type=int, default=16)
    ap.add_argument("--dgain", default="")
    ap.add_argument("--gamma", type=int, default=100)
    ap.add_argument("--out", default="snap.png")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)
    cam = None
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), f"{cam.w}x{cam.h}")
        cam.play()

        if args.dgain:
            r, g, b = (int(x) for x in args.dgain.split(","))
            mvsdk.CameraSetGain(cam.hcam, r, g, b)
        mvsdk.CameraSetGamma(cam.hcam, args.gamma)
        time.sleep(0.6)  # ISP LUT 稳态

        if args.exp_us > 0:
            cam.set_ae(False)
            cam.set_exposure(args.exp_us)
            cam.set_gain(args.gain)
            for _ in range(10):
                cam.grab()
            print(f"固定曝光: {args.exp_us/1000:.1f}ms gain={args.gain}")
        else:
            cam.set_ae(False)
            cam.set_ae(True, args.ae_target)
            t_end = time.time() + args.ae_sec
            while time.time() < t_end:
                cam.grab()
            exp_us, gain = cam.params()
            cam.set_ae(False)  # 冻结
            for _ in range(10):
                cam.grab()
            print(f"AE 收敛冻结: exp={exp_us/1000:.1f}ms gain={gain}")

        frame = cam.grab()
        cv2.imwrite(args.out, frame)
        print(f"已存 {args.out} mean={frame.mean():.1f} shape={frame.shape}")
        return 0
    except KeyboardInterrupt:
        print("收到中断信号")
        return 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}")
        return 3
    finally:
        if cam is not None:
            try:
                cam.close()
            except BaseException as e:
                print("关闭清理异常(忽略):", e)
        print("已安全退出")


if __name__ == "__main__":
    sys.exit(main())
