#!/usr/bin/env python3
"""MindVision SUA202GC 单帧采集测试（ARAvis + OpenCV）

用法: /usr/bin/python3 mv_camera_grab.py [输出文件.png]
注意: 必须用系统 Python (/usr/bin/python3) 运行，miniforge 环境没有 gi。
"""
import sys

import cv2
import numpy as np
import gi

gi.require_version("Aravis", "0.8")
from gi.repository import Aravis  # noqa: E402

# GenICam(PFNC) 与 OpenCV 的 Bayer 命名差一格相位，以下映射经合成数据实测验证
BAYER_CONV = {
    Aravis.PIXEL_FORMAT_BAYER_RG_8: cv2.COLOR_BayerBG2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GR_8: cv2.COLOR_BayerGB2BGR,
    Aravis.PIXEL_FORMAT_BAYER_GB_8: cv2.COLOR_BayerGR2BGR,
    Aravis.PIXEL_FORMAT_BAYER_BG_8: cv2.COLOR_BayerRG2BGR,
}


def main(out_path: str = "frame.png") -> None:
    Aravis.update_device_list()
    n = Aravis.get_n_devices()
    print(f"发现 {n} 台相机")
    for i in range(n):
        print("  -", Aravis.get_device_id(i))
    if n == 0:
        sys.exit("错误：没有发现相机")

    cam = Aravis.Camera.new(None)
    try:
        cam.set_pixel_format_from_string("RGB8")
    except Exception as exc:
        print(f"RGB8 设置失败，使用默认格式: {exc}")
    print("像素格式:", cam.get_pixel_format_as_string())

    payload = cam.get_payload()
    stream = cam.create_stream(None, None)
    stream.push_buffer(Aravis.Buffer.new_allocate(payload))

    print("开始采集...")
    cam.start_acquisition()
    buf = stream.pop_buffer()
    cam.stop_acquisition()
    if buf is None:
        sys.exit("错误：未取到图像帧")

    pf = buf.get_image_pixel_format()
    w, h = buf.get_image_width(), buf.get_image_height()
    pad = buf.get_image_padding()
    if hasattr(pad, "x_padding"):  # 新版 aravis 返回 (x_padding, y_padding)
        pad = pad.x_padding
    print(f"帧信息: {w}x{h}, padding={pad}, pf=0x{pf:08x}, 状态={buf.get_status()}")

    data = buf.get_image_data()
    try:
        arr = np.frombuffer(data, dtype=np.uint8)
    except TypeError:
        arr = np.frombuffer(bytes(data), dtype=np.uint8)

    if pf == Aravis.PIXEL_FORMAT_RGB_8_PACKED:
        stride = w * 3 + pad
        bgr = arr.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)[:, :, ::-1]
    elif pf == Aravis.PIXEL_FORMAT_BGR_8_PACKED:
        stride = w * 3 + pad
        bgr = arr.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)
    elif pf == Aravis.PIXEL_FORMAT_MONO_8:
        stride = w + pad
        bgr = arr.reshape(h, stride)[:, :w]
    elif pf in BAYER_CONV:
        stride = w + pad
        gray = arr.reshape(h, stride)[:, :w]
        bgr = cv2.cvtColor(gray, BAYER_CONV[pf])
    else:
        sys.exit(f"暂不支持的像素格式 0x{pf:08x}，请把以上输出发给大小姐分析")

    cv2.imwrite(out_path, bgr)
    print("已保存:", out_path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "frame.png")
