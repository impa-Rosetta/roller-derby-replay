#!/usr/bin/env python3
"""MindVision SUA202GC 实时预览与参数调节（ARAvis + OpenCV）

用法: /usr/bin/python3 mv_camera_viewer.py
滑块: WB R/B  —— 白平衡增益（软件后处理，1.00 为不调整）
      Exposure —— 相机曝光时间（对数映射，实时写相机）
      Gain     —— 相机增益（实时写相机）
按键: a=自动白平衡(灰色世界)  r=重置白平衡  s=保存当前帧  q/ESC=退出

注意: 必须用系统 Python (/usr/bin/python3) 运行（miniforge 环境没有 gi）。
      退出请用 q/ESC/关闭窗口/Ctrl+C——脚本会优雅停止取流。
      绝不要在取流中强杀本进程，可能卡死 USB 控制器导致键鼠掉线！
"""
import os
import signal
import sys
import time

import cv2
import numpy as np
import gi

gi.require_version("Aravis", "0.8")
from gi.repository import Aravis

# GenICam(PFNC) 与 OpenCV 的 Bayer 命名差一格相位，映射经合成数据实测验证
BAYER_CONV = {
    Aravis.PIXEL_FORMAT_BAYER_RG_8: cv2.COLOR_BayerBG2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GR_8: cv2.COLOR_BayerGB2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GB_8: cv2.COLOR_BayerGR2BGR,
    Aravis.PIXEL_FORMAT_BAYER_BG_8: cv2.COLOR_BayerRG2BGR,
}

WIN = "MindVision SUA202GC viewer"
N_BUFFERS = 8
DISPLAY_SCALE = 0.5
WB_SLIDER_MAX = 400  # 白平衡滑块上限，x0.01（即 4.0 倍）
GAIN_SLIDER_MIN = 100  # 相机增益滑块，x0.01

running = True


def _on_signal(_sig, _frame):
    global running
    running = False


def decode(buf):
    """ARAvis buffer → BGR 图像（Bayer 解马赛克）"""
    w, h = buf.get_image_width(), buf.get_image_height()
    pad = buf.get_image_padding()
    if hasattr(pad, "x_padding"):
        pad = pad.x_padding
    data = buf.get_image_data()
    try:
        arr = np.frombuffer(data, dtype=np.uint8)
    except TypeError:
        arr = np.frombuffer(bytes(data), dtype=np.uint8)
    pf = buf.get_image_pixel_format()
    if pf in BAYER_CONV:
        gray = arr.reshape(h, w + pad)[:, :w]
        return cv2.cvtColor(gray, BAYER_CONV[pf])
    if pf == Aravis.PIXEL_FORMAT_MONO_8:
        return cv2.cvtColor(arr.reshape(h, w + pad)[:, :w], cv2.COLOR_GRAY2BGR)
    sys.exit(f"暂不支持的像素格式 0x{pf:08x}")


def build_wb_lut(r_gain, b_gain):
    """白平衡查找表（BGR 三通道），增益变化时重建"""
    ramp = np.arange(256, dtype=np.float32)
    lut = np.stack(
        [np.clip(ramp * b_gain, 0, 255), ramp, np.clip(ramp * r_gain, 0, 255)],
        axis=-1,
    )
    return lut.astype(np.uint8).reshape(256, 1, 3)


def exp_slider_to_us(v, lo, hi):
    return lo * (hi / lo) ** (v / 100.0)


def exp_us_to_slider(us, lo, hi):
    return int(round(100 * np.log(us / lo) / np.log(hi / lo)))


def main():
    global running
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    Aravis.update_device_list()
    if Aravis.get_n_devices() == 0:
        sys.exit("错误：没有发现相机")
    device_id = Aravis.get_device_id(0)

    cam = Aravis.Camera.new(None)
    try:
        cam.set_exposure_time_auto(Aravis.Auto.OFF)
    except Exception as exc:
        print("自动曝光关闭失败:", exc)
    exp_lo, exp_hi = cam.get_exposure_time_bounds()
    gain_lo, gain_hi = cam.get_gain_bounds()
    exp_now = cam.get_exposure_time()
    gain_now = cam.get_gain()
    try:
        model = cam.get_model_name()
    except Exception:
        model = device_id  # 固件未暴露 DeviceModelName，用 U3V 设备 ID
    print(
        f"相机: {model} | 曝光 {exp_now:.0f}us "
        f"范围[{exp_lo:.1f}, {exp_hi:.0f}] | 增益 {gain_now:.2f} "
        f"范围[{gain_lo:.2f}, {gain_hi:.2f}]"
    )

    payload = cam.get_payload()
    stream = cam.create_stream(None, None)
    try:
        stream.timeout = 2000  # ms，防止 pop_buffer 永久阻塞
    except AttributeError:
        print("警告: 无法设置流超时")

    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    noop = lambda _v: None  # noqa: E731
    cv2.createTrackbar("WB R x0.01", WIN, 100, WB_SLIDER_MAX, noop)
    cv2.createTrackbar("WB B x0.01", WIN, 100, WB_SLIDER_MAX, noop)
    cv2.createTrackbar(
        "Exposure(log)", WIN, exp_us_to_slider(exp_now, exp_lo, exp_hi), 100, noop
    )
    cv2.createTrackbar(
        "Gain x0.01", WIN, int(round(gain_now * 100)), int(gain_hi * 100), noop
    )

    for _ in range(N_BUFFERS):
        stream.push_buffer(Aravis.Buffer.new_allocate(payload))

    fps = 0.0
    t_prev = time.time()
    dropped = 0
    empty = 0
    lut = None
    lut_key = None
    last_exp, last_gain = exp_now, gain_now

    print("开始取流 (q/ESC 退出)...")
    try:
        cam.start_acquisition()
        while running:
            buf = stream.pop_buffer()
            if buf is None:
                empty += 1
                if empty > 30:
                    print("长时间无帧，退出")
                    break
                continue
            empty = 0
            stream.push_buffer(Aravis.Buffer.new_allocate(payload))
            if buf.get_status() != Aravis.BufferStatus.SUCCESS:
                dropped += 1
                continue

            bgr = decode(buf)

            r_gain = cv2.getTrackbarPos("WB R x0.01", WIN) / 100.0
            b_gain = cv2.getTrackbarPos("WB B x0.01", WIN) / 100.0
            if (r_gain, b_gain) != lut_key:
                lut = build_wb_lut(r_gain, b_gain)
                lut_key = (r_gain, b_gain)
            if lut is not None:
                bgr = cv2.LUT(bgr, lut)

            # 相机参数写回（拖动时超过 1% 变化才写，避免刷寄存器）
            exp_v = cv2.getTrackbarPos("Exposure(log)", WIN)
            exp_new = exp_slider_to_us(exp_v, exp_lo, exp_hi)
            if abs(exp_new - last_exp) > last_exp * 0.01:
                try:
                    cam.set_exposure_time(exp_new)
                    last_exp = exp_new
                except Exception as exc:
                    print("设置曝光失败:", exc)
            gain_new = cv2.getTrackbarPos("Gain x0.01", WIN) / 100.0
            if abs(gain_new - last_gain) > 0.01:
                try:
                    cam.set_gain(gain_new)
                    last_gain = gain_new
                except Exception as exc:
                    print("设置增益失败:", exc)

            t_now = time.time()
            dt = t_now - t_prev
            t_prev = t_now
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

            disp = cv2.resize(
                bgr, None, fx=DISPLAY_SCALE, fy=DISPLAY_SCALE,
                interpolation=cv2.INTER_AREA,
            )
            cv2.imshow(WIN, disp)
            cv2.setWindowTitle(
                WIN,
                f"{fps:.1f}fps | R={r_gain:.2f} B={b_gain:.2f} | "
                f"exp={exp_new / 1000:.1f}ms g={gain_new:.2f} | miss={dropped}",
            )

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("s"):
                fn = time.strftime("frame_%Y%m%d_%H%M%S.png")
                cv2.imwrite(fn, bgr)
                print("已保存:", os.path.abspath(fn))
            elif key == ord("a"):
                means = bgr.mean(axis=(0, 1))
                if means.min() > 0.5:
                    nb = np.clip(b_gain * means[1] / means[0], 0.01, WB_SLIDER_MAX / 100)
                    nr = np.clip(r_gain * means[1] / means[2], 0.01, WB_SLIDER_MAX / 100)
                    cv2.setTrackbarPos("WB B x0.01", WIN, int(round(nb * 100)))
                    cv2.setTrackbarPos("WB R x0.01", WIN, int(round(nr * 100)))
                    print(f"自动白平衡: R={nr:.2f} B={nb:.2f}")
                else:
                    print("画面过暗，自动白平衡不可靠")
            elif key == ord("r"):
                cv2.setTrackbarPos("WB B x0.01", WIN, 100)
                cv2.setTrackbarPos("WB R x0.01", WIN, 100)

            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        print("正在停止取流...")
        try:
            cam.stop_acquisition()
        except Exception as exc:
            print("stop_acquisition 出错:", exc)
        cv2.destroyAllWindows()
        print("已安全退出")


if __name__ == "__main__":
    main()
