#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jetson 端：海康相机 JPEG 推流（本机远程预览/调焦）

监听 TCP，等本机 hk_stream_view.py 连上来后，持续推送缩放并 JPEG 压缩的帧。

用法:
  source /etc/profile.d/mvs_env.sh
  /usr/bin/python3 hk_stream_server.py [--port 9000] [--width 1280] [--quality 80]
"""
import argparse
import ctypes
import os
import signal
import socket
import struct
import sys
import time

import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_SOFTWARE_LIBENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_GENICAM_CLPROTOCOL', '/opt/MVS/lib/CLProtocol')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

import cv2  # noqa: E402

# 与项目脚本一致：海康 BayerGR8 用 OpenCV 的 BayerGB 解析
BAYER2BGR = {
    PixelType_Gvsp_BayerGR8: cv2.COLOR_BayerGB2BGR,
    PixelType_Gvsp_BayerGB8: cv2.COLOR_BayerGR2BGR,
    PixelType_Gvsp_BayerRG8: cv2.COLOR_BayerBG2BGR,
    PixelType_Gvsp_BayerBG8: cv2.COLOR_BayerRG2BGR,
}

_stop = False


def _on_term(signum, frame):
    global _stop
    _stop = True


def open_camera():
    dev_list = MV_CC_DEVICE_INFO_LIST()
    MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dev_list)
    dev_info = None
    for i in range(dev_list.nDeviceNum):
        info = dev_list.pDeviceInfo[i].contents
        if info.nTLayerType == MV_USB_DEVICE:
            dev_info = info
            break
    if dev_info is None:
        raise RuntimeError('没有找到 USB 相机')

    cam = MvCamera()
    cam.MV_CC_CreateHandle(dev_info)
    cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    cam.MV_CC_SetEnumValue('TriggerMode', MV_TRIGGER_MODE_OFF)

    pixel = None
    for pt in (PixelType_Gvsp_BayerGR8, PixelType_Gvsp_BayerGB8,
               PixelType_Gvsp_BayerRG8, PixelType_Gvsp_BayerBG8):
        if cam.MV_CC_SetEnumValue('PixelFormat', pt) == 0:
            pixel = pt
            break
    if pixel is None:
        raise RuntimeError('Bayer 格式设置失败')

    cam.MV_CC_SetEnumValue('ExposureAuto', 2)
    cam.MV_CC_SetEnumValue('GainAuto', 2)
    cam.MV_CC_SetBoolValue('AcquisitionFrameRateEnable', True)
    cam.MV_CC_SetFloatValue('AcquisitionFrameRate', 60.0)

    st = MVCC_INTVALUE_EX()
    ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_INTVALUE_EX))
    cam.MV_CC_GetIntValueEx('PayloadSize', st)
    return cam, pixel, st.nCurValue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9000)
    ap.add_argument('--width', type=int, default=1280,
                    help='推送宽度（等比缩放，默认 1280）')
    ap.add_argument('--quality', type=int, default=80)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('::', args.port))
    srv.listen(1)
    print('推流服务监听 [::]:%d，等待本机连接...' % args.port, flush=True)

    cam = None
    grabbed = False
    conn = None
    try:
        cam, pixel, payload = open_camera()
        buf = (ctypes.c_ubyte * payload)()
        info = MV_FRAME_OUT_INFO_EX()
        cam.MV_CC_StartGrabbing()
        grabbed = True
        print('相机已启动，等待客户端...', flush=True)

        srv.settimeout(60.0)
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            print('等待连接超时，退出', flush=True)
            return 2
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print('已连接 %s:%s，开始推流' % (addr[0], addr[1]), flush=True)

        n = 0
        t0 = time.perf_counter()
        while not _stop:
            ret = cam.MV_CC_GetOneFrameTimeout(
                ctypes.byref(buf), payload, info, 1000)
            if ret != 0:
                continue
            w, h = info.nWidth, info.nHeight
            frame = np.frombuffer(
                bytes(buf[:info.nFrameLen]), np.uint8).reshape(h, w)
            bgr = cv2.cvtColor(frame, BAYER2BGR[pixel])
            if w > args.width:
                bgr = cv2.resize(
                    bgr, (args.width, int(h * args.width / w)),
                    interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(
                '.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
            if not ok:
                continue
            data = jpg.tobytes()
            try:
                conn.sendall(struct.pack('<I', len(data)) + data)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                print('客户端断开', flush=True)
                break
            n += 1
            if n % 60 == 0:
                print('已推 %d 帧 (%.1f fps)'
                      % (n, n / (time.perf_counter() - t0)), flush=True)
    except Exception as e:
        print('错误: %s: %s' % (type(e).__name__, e), flush=True)
        return 3
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if cam is not None:
            try:
                if grabbed:
                    cam.MV_CC_StopGrabbing()
                cam.MV_CC_CloseDevice()
                cam.MV_CC_DestroyHandle()
            except BaseException as e:
                print('清理异常(忽略):', e, flush=True)
        srv.close()
        print('推流服务已退出', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
