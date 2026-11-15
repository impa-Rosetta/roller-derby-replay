#!/usr/bin/env python3
"""MindVision SUA202GC 补光验证脚本（MVSDK）

2026-10-12 事故教训：取帧超时未处理 → 脚本崩溃 → xHCI 控制器假死。
本脚本采取全部防御措施：
  1. 所有取帧带超时 + 有限重试，绝不因单帧超时崩掉
  2. 相机 buffer 释放用 try/finally，任何路径都不泄漏
  3. 一切异常走统一的 finally 优雅关闭（CameraUnInit + CameraAlignFree）
  4. 顶层兜底：任何未知异常也只打印错误、正常退出
  5. SIGTERM 同样走优雅关闭（除 Ctrl+C 外的第二道保险）

验证目标：手电筒补光前后对比（需在交互终端运行，轮间手动开关手电筒）
  - AE 自动曝光收敛到的曝光/增益（补光有效 → 曝光从 256ms 大幅缩短）
  - 6ms 固定曝光下的画面亮度（无补光时近黑 mean≈8）
  - 6ms 固定曝光下的实测 FPS（暗光拖慢 AE → 帧率被 1/曝光 上限压制）

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_camera_test.py [--gain 16] [--save 前缀]
"""
import argparse
import signal
import sys
import time
from collections import deque

import cv2
import numpy as np
import mvsdk

DEFAULT_TIMEOUT_MS = 1000
GRAB_RETRIES = 5
AE_SECONDS = 15      # AE 收敛观察时长（两轮同预算，公平对比）
FAST_SECONDS = 5     # 6ms 满速测试时长
BRIGHT_WINDOW = 10   # 亮度统计的滚动帧窗


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


class Camera:
    """相机会话封装：任何失败路径都保证资源释放"""

    def __init__(self):
        devs = mvsdk.CameraEnumerateDevice()
        if not devs:
            raise RuntimeError("没有发现相机")
        self.dev = devs[0]
        self.hcam = None
        self.pfb = None
        self._open()

    def _open(self):
        self.hcam = mvsdk.CameraInit(self.dev, -1, -1)
        try:
            cap = mvsdk.CameraGetCapability(self.hcam)
            self.w = cap.sResolutionRange.iWidthMax
            self.h = cap.sResolutionRange.iHeightMax
            mvsdk.CameraSetIspOutFormat(self.hcam, mvsdk.CAMERA_MEDIA_TYPE_BGR8)
            mvsdk.CameraSetTriggerMode(self.hcam, 0)
            self.pfb = mvsdk.CameraAlignMalloc(self.w * self.h * 3, 16)
        except BaseException:
            self.close()
            raise

    def grab(self, timeout_ms=DEFAULT_TIMEOUT_MS, retries=GRAB_RETRIES):
        """取一帧。超时/错误重试有限次；无论成败都释放相机 buffer"""
        last = None
        for _ in range(retries):
            try:
                praw, fh = mvsdk.CameraGetImageBuffer(self.hcam, timeout_ms)
            except mvsdk.CameraException as e:
                last = e
                continue  # 超时或其它错误：重试
            try:
                mvsdk.CameraImageProcess(self.hcam, praw, self.pfb, fh)
            finally:
                mvsdk.CameraReleaseImageBuffer(self.hcam, praw)
            data = (mvsdk.c_ubyte * fh.uBytes).from_address(self.pfb)
            return np.frombuffer(data, dtype=np.uint8).reshape(
                (fh.iHeight, fh.iWidth,
                 1 if fh.uiMediaType == mvsdk.CAMERA_MEDIA_TYPE_MONO8 else 3)
            )
        msg = getattr(last, "message", str(last))
        raise RuntimeError(f"连续 {retries} 次取帧失败: {msg}")

    def set_ae(self, on, target=120):
        mvsdk.CameraSetAeState(self.hcam, 1 if on else 0)
        if on:
            mvsdk.CameraSetAeTarget(self.hcam, target)

    def set_exposure(self, us):
        mvsdk.CameraSetExposureTime(self.hcam, us)

    def set_gain(self, g):
        mvsdk.CameraSetAnalogGain(self.hcam, int(g))

    def params(self):
        """当前 (曝光时间 us, 模拟增益)"""
        return (mvsdk.CameraGetExposureTime(self.hcam),
                mvsdk.CameraGetAnalogGain(self.hcam))

    def play(self):
        mvsdk.CameraPlay(self.hcam)

    def close(self):
        if self.hcam is not None:
            h, self.hcam = self.hcam, None
            try:
                mvsdk.CameraUnInit(h)
            except Exception as e:
                print("CameraUnInit 出错(忽略):", e)
        if self.pfb is not None:
            p, self.pfb = self.pfb, None
            try:
                mvsdk.CameraAlignFree(p)
            except Exception as e:
                print("CameraAlignFree 出错(忽略):", e)


def measure(cam, tag, prompt, gain, save_prefix):
    """一轮测量：AE 收敛观察 + 6ms 满速测试。返回统计 dict"""
    input(prompt)

    print(f"[{tag}] AE 自动曝光收敛中（{AE_SECONDS}s）...")
    cam.set_ae(False)  # 先复位，确保 AE 从头收敛
    cam.set_ae(True, 120)
    recent = deque(maxlen=BRIGHT_WINDOW)
    n = 0
    t_end = time.time() + AE_SECONDS
    while time.time() < t_end:
        recent.append(cam.grab())
        n += 1
    ae_exp_us, ae_gain = cam.params()
    ae_bright = float(np.mean([f.mean() for f in recent]))
    print(f"[{tag}] AE 收敛: exp={ae_exp_us/1000:.1f}ms gain={ae_gain} "
          f"亮度 mean={ae_bright:.1f}（{n}帧≈{n/AE_SECONDS:.1f}FPS）")
    if save_prefix:
        cv2.imwrite(f"{save_prefix}_ae.png", recent[-1])

    print(f"[{tag}] 6ms+{gain:g}x 满速测试（{FAST_SECONDS}s）...")
    cam.set_ae(False)
    cam.set_exposure(6000)
    cam.set_gain(gain)
    for _ in range(5):  # 等参数生效
        cam.grab()
    recent.clear()
    n = 0
    t0 = time.time()
    while time.time() - t0 < FAST_SECONDS:
        recent.append(cam.grab())
        n += 1
    dur = time.time() - t0
    fps = n / dur
    fast_bright = float(np.mean([f.mean() for f in recent]))
    print(f"[{tag}] 6ms 实测: {fps:.1f} FPS 亮度 mean={fast_bright:.1f}（{n}帧）")
    if save_prefix:
        cv2.imwrite(f"{save_prefix}_fast.png", recent[-1])

    return dict(ae_exp_ms=ae_exp_us / 1000, ae_gain=ae_gain,
                ae_bright=ae_bright, fps=fps, fast_bright=fast_bright)


def report(r1, r2):
    print("\n===== 补光验证对比 =====")

    def line(name, a, b, unit=""):
        print(f"{name}: 无补光 {a}{unit} → 补光 {b}{unit}")

    line("AE 收敛曝光", f"{r1['ae_exp_ms']:.1f}", f"{r2['ae_exp_ms']:.1f}", " ms")
    line("AE 收敛增益", f"{r1['ae_gain']:g}", f"{r2['ae_gain']:g}", "x")
    line("AE 画面亮度", f"{r1['ae_bright']:.1f}", f"{r2['ae_bright']:.1f}")
    line("6ms 画面亮度", f"{r1['fast_bright']:.1f}", f"{r2['fast_bright']:.1f}")
    line("6ms 实测 FPS", f"{r1['fps']:.1f}", f"{r2['fps']:.1f}")
    print("========================")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain", type=int, default=16,
                    help="6ms 测试用模拟增益（默认 16，MVSDK 只收整数）")
    ap.add_argument("--save", default="",
                    help="存图前缀（存 前缀_nolum/lum_ae/fast.png 共 4 张）")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), f"{cam.w}x{cam.h}")
        cam.play()

        save = args.save
        r1 = measure(cam, "无补光",
                     ">>> 保持手电筒关闭，按 Enter 开始第 1 轮...",
                     args.gain, (save + "_nolum") if save else "")
        r2 = measure(cam, "补光",
                     ">>> 打开手电筒照向场景，按 Enter 开始第 2 轮...",
                     args.gain, (save + "_lum") if save else "")
        report(r1, r2)
    except KeyboardInterrupt:
        print("收到中断信号（Ctrl+C/SIGTERM）")
        return 130
    except mvsdk.CameraException as e:
        print(f"MVSDK 错误: {e.error_code} {e.message}")
        return 2
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
