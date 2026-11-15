#!/usr/bin/env python3
"""色卡灰阶中性闭环校准（MVSDK / SUA202GC）

原理（沿用 2026-10-12 收敛的经验）：
  1. AE 收敛后冻结，曝光 ×2 提升采样信噪比（白平衡与曝光无关）
  2. 对 R/G/B 各 +30% 探测自身响应指数 k（显示域响应超线性）
  3. 以灰阶块（白/浅/中/深灰）"应中性"为目标，阻尼迭代数字增益
  4. 每轮等待 0.6s ISP LUT 稳态再测量

用法:
  cd /home/ifit/mvsdk/demo/python_demo
  PYTHONPATH=$PWD LD_LIBRARY_PATH=/home/ifit/mvsdk/lib/arm64 /usr/bin/python3 /home/ifit/xjh/mv_color_calib.py
"""
import signal
import sys
import time

import cv2
import numpy as np
import mvsdk

from mv_camera_test import Camera

# 卡片四角（原图坐标：左上 右上 右下 左下）
QUAD = np.array([[530, 516], [1312, 512], [1318, 736], [534, 740]], np.float32)
WARP_W, WARP_H = 1600, 452

# 灰阶块在正视图中的中心 x（y 带固定）
PATCHES = [("W", 88), ("LG", 232), ("MG", 336), ("DG1", 448), ("DG2", 552), ("DG3", 664)]
PY0, PY1, PHALF = 350, 385, 24

SETTLE_SEC = 0.6
DAMP = 0.9
MAX_ITER = 6
TOL = 4.0
DGAIN_MIN, DGAIN_MAX = 1, 400


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def warp_frame(frame):
    dst = np.array([[0, 0], [WARP_W - 1, 0], [WARP_W - 1, WARP_H - 1], [0, WARP_H - 1]], np.float32)
    m = cv2.getPerspectiveTransform(QUAD, dst)
    return cv2.warpPerspective(frame, m, (WARP_W, WARP_H), flags=cv2.INTER_CUBIC)


def wait_s(cam, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        cam.grab()


def sample(cam, n=5):
    acc = {name: [] for name, _ in PATCHES}
    for _ in range(n):
        f = warp_frame(cam.grab()).astype(np.float64)
        for name, cx in PATCHES:
            p = f[PY0:PY1, cx - PHALF:cx + PHALF]
            acc[name].append(p.reshape(-1, 3).mean(axis=0))
    return {k: np.array(v).mean(axis=0) for k, v in acc.items()}  # BGR


def dev_of(patches):
    devs = {}
    for name, (b, g, r) in patches.items():
        m = (b + g + r) / 3
        devs[name] = max(abs(b - m), abs(g - m), abs(r - m)) / m * 100 if m > 2 else 0.0
    return float(np.mean(list(devs.values()))), devs


def fmt(patches):
    return {k: tuple(round(float(v), 1) for v in bgr) for k, bgr in patches.items()}


def apply_gain(cam, dgain):
    r, g, b = dgain
    mvsdk.CameraSetGain(cam.hcam, int(round(r)), int(round(g)), int(round(b)))


def main():
    signal.signal(signal.SIGTERM, _on_term)
    cam = None
    try:
        cam = Camera()
        print("相机:", cam.dev.GetFriendlyName(), f"{cam.w}x{cam.h}")
        cam.play()

        print("AE 收敛...")
        cam.set_ae(False)
        cam.set_ae(True, 100)
        wait_s(cam, 4.0)
        exp_us, gain = cam.params()
        cam.set_ae(False)
        exp = min(int(exp_us * 2), 256000)  # 曝光 ×2 提信噪比（不影响白平衡）
        cam.set_exposure(exp)
        cam.set_gain(gain)
        wait_s(cam, 0.5)
        print(f"冻结: exp={exp/1000:.1f}ms(原{exp_us/1000:.1f}) gain={gain}")

        apply_gain(cam, (100, 100, 100))
        wait_s(cam, SETTLE_SEC)
        base = sample(cam)
        dev0, devs0 = dev_of(base)
        print(f"\n[基准 dgain(100,100,100)] 平均偏色={dev0:.1f}%  BGR:")
        for k in base:
            print(f"  {k}: {fmt(base)[k]}")

        # 响应探测：各通道 +30%，测自身显示域响应
        print("\n响应探测...")
        ks = {}
        for ch, argidx, bgr_idx in [("R", 0, 2), ("G", 1, 1), ("B", 2, 0)]:
            probe = [100, 100, 100]
            probe[argidx] = 130
            apply_gain(cam, probe)
            wait_s(cam, SETTLE_SEC)
            p = sample(cam)
            ratios = [p[n][bgr_idx] / base[n][bgr_idx] for n in base if base[n][bgr_idx] > 2]
            ratio = float(np.exp(np.mean(np.log(ratios))))
            k = np.log(ratio) / np.log(1.3)
            ks[ch] = k
            print(f"  {ch}: 响应比={ratio:.3f}  k={k:.2f}")
        apply_gain(cam, (100, 100, 100))
        wait_s(cam, SETTLE_SEC)

        # 阻尼迭代
        dgain = np.array([100.0, 100.0, 100.0])
        best = None
        for it in range(1, MAX_ITER + 1):
            apply_gain(cam, dgain)
            wait_s(cam, SETTLE_SEC)
            patches = sample(cam)
            dev, devs = dev_of(patches)
            print(f"\n[iter{it}] dgain=({dgain[0]:.0f},{dgain[1]:.0f},{dgain[2]:.0f}) "
                  f"平均偏色={dev:.1f}%  " +
                  " ".join(f"{k}={v:.1f}%" for k, v in devs.items()))
            for k in patches:
                print(f"  {k}: {fmt(patches)[k]}")
            if best is None or dev < best[0]:
                best = (dev, dgain.copy(), patches)
            if dev < TOL:
                print("收敛")
                break

            # 每块"应中性"目标 → 各块每通道所需倍率 → 几何均值 → 线性域
            tb, tg, tr = [], [], []
            for name, (b, g, r) in patches.items():
                m = (b + g + r) / 3
                if m > 5:
                    tb.append(m / max(b, 1e-3))
                    tg.append(m / max(g, 1e-3))
                    tr.append(m / max(r, 1e-3))

            def geo(v):
                return float(np.exp(np.mean(np.log(v))))

            lin = np.array([
                geo(tr) ** (1.0 / ks["R"]),
                geo(tg) ** (1.0 / ks["G"]),
                geo(tb) ** (1.0 / ks["B"]),
            ])
            new_gain = np.clip(dgain * lin ** DAMP, DGAIN_MIN, DGAIN_MAX)
            print(f"  线性域倍率=({lin[0]:.3f},{lin[1]:.3f},{lin[2]:.3f}) → "
                  f"下一次 dgain=({new_gain[0]:.0f},{new_gain[1]:.0f},{new_gain[2]:.0f})")
            if np.allclose(new_gain, dgain, atol=0.5):
                print("增益不再变化，停止")
                break
            dgain = new_gain

        # 应用最优值，存校准后快照
        best_dev, best_gain, best_patches = best
        apply_gain(cam, best_gain)
        wait_s(cam, SETTLE_SEC)
        final = sample(cam)
        frame = cam.grab()
        cv2.imwrite("calib_after.png", frame)
        vis = warp_frame(frame)
        for name, cx in PATCHES:
            cv2.rectangle(vis, (cx - PHALF, PY0), (cx + PHALF, PY1), (0, 255, 0), 2)
        cv2.imwrite("calib_after_warp.png", vis)
        frame0 = cam.grab()  # 占位，避免未用变量警告
        del frame0

        print("\n===== 结果 =====")
        print(f"最优 dgain (R,G,B) = ({best_gain[0]:.0f}, {best_gain[1]:.0f}, {best_gain[2]:.0f})")
        print(f"平均偏色 {dev0:.1f}% → {best_dev:.1f}%")
        fd, fdevs = dev_of(final)
        print(f"终检偏色: {fd:.1f}%  " + " ".join(f"{k}={v:.1f}%" for k, v in fdevs.items()))
        for k in final:
            print(f"  {k}: {fmt(final)[k]}")
        print("已存 calib_after.png / calib_after_warp.png")
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
