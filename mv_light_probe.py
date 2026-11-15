#!/usr/bin/env python3
"""光照探针：解释"实时预览亮、6ms 录像黑"的真相

实时预览用 AE 自动曝光（暗光下自动拉长曝光到几十上百 ms），所以亮；
满帧率录像锁定 6ms（帧率=1/曝光），同一光照进光量只有 AE 的几分之一 → 黑。
本脚本实测当前光照：AE 实际用的曝光 + 各固定曝光档的画面亮度，并存对比图。

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD:/home/ifit/xjh LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /tmp/light_probe.py
"""
import signal
import sys
import time

import cv2
import numpy as np
import mvsdk
from mv_camera_test import Camera

CAL_DGAIN = (107, 95, 98)  # 当前校准数字增益（2026-10-14 换镜头后）
EXPOSURE_LADDER_US = (6000, 16500, 33000, 66000)  # ≤166 / ≤60 / ≤30 / ≤15 fps
SHOT = "/home/ifit/xjh/light_probe_{:.1f}ms.png"


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def wait_s(cam, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        cam.grab()


def main():
    signal.signal(signal.SIGTERM, _on_term)
    cam = None
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), flush=True)
        cam.play()
        mvsdk.CameraSetGain(cam.hcam, *CAL_DGAIN)
        mvsdk.CameraSetGamma(cam.hcam, 100)

        # 1) AE 收敛——实时预览"亮"的真相
        cam.set_ae(True, 100)
        wait_s(cam, 3.5)
        exp_us, again = cam.params()
        f = cam.grab()
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        print(f"AE 模式: 曝光={exp_us / 1000:7.2f}ms 模拟增益={again} "
              f"→ mean={f.mean():5.1f} p99={np.percentile(g, 99):3.0f}"
              f"  （实时预览就长这样：亮）", flush=True)
        cam.set_ae(False)

        # 2) 固定曝光阶梯（16x 模拟 + 校准数字增益，与录像同款参数）
        for e_us in EXPOSURE_LADDER_US:
            cam.set_exposure(e_us)
            cam.set_gain(16)
            wait_s(cam, 0.8)
            f = cam.grab()
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            dark = (g < 12).mean() * 100
            fn = SHOT.format(e_us / 1000)
            cv2.imwrite(fn, f)
            print(f"{e_us / 1000:6.1f}ms (帧率上限 {1000 / (e_us / 1000):5.1f}fps): "
                  f"mean={f.mean():5.1f} p99={np.percentile(g, 99):3.0f} "
                  f"暗部占比={dark:4.1f}%  → {fn}", flush=True)

        # 3) 结论
        print(f"\n结论: 当前光照 AE 要用 {exp_us / 1000:.1f}ms；满帧率锁 6ms，"
              f"进光量只有它的 1/{exp_us / 6000:.1f} → 所以黑。"
              f"降低录制帧率（长曝光）就能接近实时预览亮度。", flush=True)
    except KeyboardInterrupt:
        print("中断", flush=True)
        return 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}", flush=True)
        return 3
    finally:
        if cam is not None:
            try:
                cam.close()
            except BaseException as e:
                print("清理异常(忽略):", e, flush=True)
        print("已安全退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
