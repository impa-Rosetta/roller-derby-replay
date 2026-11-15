#!/usr/bin/env python3
"""MindVision SUA202GC 画面调优：自动曝光/增益 + 实测亮度 + 软件白平衡

用法: /usr/bin/python3 mv_camera_tune.py [输出.png]
注意: 运行前先关闭 arv-viewer（相机同一时间只能被一个程序控制）。
相机只输出 Bayer 原始数据，白平衡只能在软件端做（本脚本自动做灰度世界校正）。
"""
import sys
import time

import cv2
import numpy as np
import gi

gi.require_version("Aravis", "0.8")
from gi.repository import Aravis  # noqa: E402

BAYER_CONV = {
    Aravis.PIXEL_FORMAT_BAYER_RG_8: cv2.COLOR_BayerRG2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GR_8: cv2.COLOR_BayerGR2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GB_8: cv2.COLOR_BayerGB2BGR,
    Aravis.PIXEL_FORMAT_BAYER_BG_8: cv2.COLOR_BayerBG2BGR,
}


def to_bgr(buf):
    pf = buf.get_image_pixel_format()
    w, h = buf.get_image_width(), buf.get_image_height()
    pad = buf.get_image_padding()
    if hasattr(pad, "x_padding"):
        pad = pad.x_padding
    data = buf.get_image_data()
    try:
        arr = np.frombuffer(data, dtype=np.uint8)
    except TypeError:
        arr = np.frombuffer(bytes(data), dtype=np.uint8)
    if pf == Aravis.PIXEL_FORMAT_RGB_8_PACKED:
        stride = w * 3 + pad
        return arr.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)[:, :, ::-1]
    if pf == Aravis.PIXEL_FORMAT_BGR_8_PACKED:
        stride = w * 3 + pad
        return arr.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)
    if pf == Aravis.PIXEL_FORMAT_MONO_8:
        stride = w + pad
        return arr.reshape(h, stride)[:, :w]
    if pf in BAYER_CONV:
        stride = w + pad
        gray = arr.reshape(h, stride)[:, :w]
        return cv2.cvtColor(gray, BAYER_CONV[pf])
    return None


def center_stats(im):
    cn = im[im.shape[0] // 4: im.shape[0] * 3 // 4,
            im.shape[1] // 4: im.shape[1] * 3 // 4]
    b, g, r = (float(v) for v in cn.mean(axis=(0, 1)))
    return b, g, r


def capture(cam, stream, payload, n=1):
    for _ in range(n):
        stream.push_buffer(Aravis.Buffer.new_allocate(payload))
    cam.start_acquisition()
    last = None
    t0 = time.time()
    for _ in range(n):
        buf = stream.pop_buffer()
        if buf is not None:
            last = buf
    dt = time.time() - t0
    cam.stop_acquisition()
    return last, n / dt


def main(out_path="tune_result.png"):
    Aravis.update_device_list()
    if Aravis.get_n_devices() == 0:
        sys.exit("没发现相机（arv-viewer 是否还占着？）")
    cam = Aravis.Camera.new(None)
    print("像素格式:", cam.get_pixel_format_as_string())

    if cam.is_exposure_auto_available():
        cam.set_exposure_time_auto(Aravis.Auto.CONTINUOUS)
        print("自动曝光: 已开启")
    if cam.is_gain_auto_available():
        cam.set_gain_auto(Aravis.Auto.CONTINUOUS)
        print("自动增益: 已开启")

    payload = cam.get_payload()
    stream = cam.create_stream(None, None)
    buf, fps = capture(cam, stream, payload, n=15)

    print(f"实测帧率: {fps:.1f} fps")
    if cam.is_exposure_auto_available():
        print(f"曝光: {cam.get_exposure_time():.0f} us (自动={cam.get_exposure_time_auto().value_nick})")
    if cam.is_gain_auto_available():
        print(f"增益: {cam.get_gain():.1f} dB (自动={cam.get_gain_auto().value_nick})")

    img = to_bgr(buf)
    if img is None:
        sys.exit("取帧失败")
    b, g, r = center_stats(img)
    print(f"画面亮度: B={b:.0f} G={g:.0f} R={r:.0f}  均值={(b + g + r) / 3:.0f} (理想 80~120)")

    # 自动力度不足时手动拨高兜底
    if (b + g + r) / 3 < 40 and cam.is_exposure_time_available():
        print("自动曝光力度不足，切换手动高曝光：")
        if cam.is_exposure_auto_available():
            cam.set_exposure_time_auto(Aravis.Auto.OFF)
        cam.set_frame_rate_enable(False)
        cam.set_exposure_time(40000.0)
        if cam.is_gain_available():
            cam.set_gain(10.0)
        buf, fps = capture(cam, stream, payload, n=5)
        print(f"手动模式: 曝光={cam.get_exposure_time():.0f} us, 增益={cam.get_gain():.1f} dB, 帧率={fps:.1f} fps")
        img = to_bgr(buf)
        b, g, r = center_stats(img)
        print(f"手动模式亮度: B={b:.0f} G={g:.0f} R={r:.0f}")

    cv2.imwrite(out_path, img)
    print("原图已保存:", out_path)

    f = img.astype(np.float32)
    means = f.reshape(-1, 3).mean(axis=0)
    f *= means.mean() / means
    wb_path = out_path.replace(".png", "_wb.png")
    cv2.imwrite(wb_path, np.clip(f, 0, 255).astype(np.uint8))
    print("软件白平衡校正版已保存:", wb_path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tune_result.png")
