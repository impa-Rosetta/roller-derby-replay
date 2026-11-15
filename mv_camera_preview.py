#!/usr/bin/env python3
"""MindVision SUA202GC 实时预览（MVSDK）——补光调试用

复用 mv_camera_test.py 的防弹 Camera 封装（取帧超时重试 / finally 释放 /
统一优雅关闭），叠加实时亮度与 FPS，边调光源边看效果。

窗口按键:
  q / ESC  退出（优雅关闭）
  e        切换 AE 自动曝光 / 6ms 满帧率模式(165FPS)
  b        执行一次性白平衡校准（拿白纸对镜头后按）
  s        存当前帧到脚本目录 preview_时间戳.png

默认启动=AE + 内置色卡校准数字增益（2026-10-12 当前灯光下收敛值）；
环境光变化时按 b 重做白平衡（白纸对镜头）或重跑色卡校准。
6ms 满帧率模式画面亮度取决于光照——光照不足时会很暗（物理定律），
数码提亮会毁色彩；赛事场景请保证照明（体育馆级）。

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_camera_preview.py
"""
import os
import signal
import sys
import time

import cv2
import mvsdk
from mv_camera_test import Camera

# 满帧率模式参数（6ms+满模拟增益）——画面亮度取决于光照，赛事场景保证照明
TUNE_EXPOSURE_US = 6000
TUNE_ANALOG_GAIN = 16
TUNE_GAMMA = 100              # ISP gamma 默认值
TUNE_SETTLE_SEC = 0.6         # 参数切换后 ISP LUT 重建等待

# 数字增益(R,G,B)——色卡校准 (80,97,122) 基础上按观感去绿：G -10% → (80,87,122)
# 原因：灰卡数值已中性，但场馆绿色环境光导致整体观感偏青绿，用户要求降绿
# 仅对当前灯光有效；光照变化时重跑 /home/ifit/xjh/mv_color_calib.py 再决定观感偏移
CAL_DGAIN = (80, 87, 122)
AE_TARGET = 100               # AE 目标亮度（100：亮度适中、高光留余量）

WIN = "SUA202GC preview  [q]uit  [e] AE/fast  [b] WB  [s] save"
SAVE_DIR = os.path.dirname(os.path.abspath(__file__))


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def wait_s(cam, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        cam.grab()


def apply_tuned(cam):
    """应用调优参数（启动/切回 tuned 模式用）"""
    cam.set_ae(False)
    cam.set_exposure(TUNE_EXPOSURE_US)
    cam.set_gain(TUNE_ANALOG_GAIN)
    mvsdk.CameraSetGain(cam.hcam, *CAL_DGAIN)
    mvsdk.CameraSetGamma(cam.hcam, TUNE_GAMMA)


def apply_ae(cam):
    """恢复 AE 自动曝光 + 色卡校准数字增益 + 默认 gamma"""
    mvsdk.CameraSetGain(cam.hcam, *CAL_DGAIN)
    mvsdk.CameraSetGamma(cam.hcam, 100)
    cam.set_ae(True, AE_TARGET)


def main():
    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), f"{cam.w}x{cam.h}")
        cam.play()
        apply_ae(cam)  # 默认 AE + 校准增益；按 e 可切满帧率模式
        tuned = False
        print("AE 收敛中（约 2 秒）...")
        wait_s(cam, 2.0)

        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WIN, 1280, 800)

        t0, n, fps = time.time(), 0, 0.0
        while True:
            frame = cam.grab()
            n += 1
            now = time.time()
            if now - t0 >= 1.0:
                fps = n / (now - t0)
                t0, n = now, 0

            exp_us, gain = cam.params()
            mode = "AE" if not tuned else f"fast {exp_us / 1000:.1f}ms"
            text = f"{mode}  mean={frame.mean():.1f}  {fps:.1f} FPS"
            cv2.putText(frame, text, (12, 40), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow(WIN, frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            try:  # 点窗口 × 时也优雅退出（防进程滞留取流）
                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    print("窗口已关闭，优雅退出")
                    break
            except cv2.error:
                break
            if key == ord('e'):
                tuned = not tuned
                if tuned:
                    apply_tuned(cam)
                else:
                    apply_ae(cam)
                wait_s(cam, TUNE_SETTLE_SEC)  # LUT 重建稳态
            elif key == ord('b'):
                mvsdk.CameraSetOnceWB(cam.hcam)
                print("已执行白平衡校准")
            elif key == ord('s'):
                fn = os.path.join(SAVE_DIR,
                                  time.strftime("preview_%H%M%S.png"))
                cv2.imwrite(fn, frame)
                print("已存", fn)
    except KeyboardInterrupt:
        print("收到中断信号（Ctrl+C/SIGTERM）")
        return 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}")
        return 3
    finally:
        cv2.destroyAllWindows()
        if cam is not None:
            try:
                cam.close()
            except BaseException as e:
                print("关闭清理异常(忽略):", e)
        print("已安全退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
