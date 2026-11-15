#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jetson 端海康相机采集验证（无需 GUI）

枚举 USB 相机 → Bayer8 → 自动曝光 → 预热后取 N 秒 → 打印实测 FPS
与相机内部 ResultingFrameRate，保存一帧 demosaic PNG（正确相位）。

用法:
  source /etc/profile.d/mvs_env.sh
  /usr/bin/python3 hk_speed_test.py [--seconds 5]
"""
import argparse
import ctypes
import os
import sys
import time

import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_SOFTWARE_LIBENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_GENICAM_CLPROTOCOL', '/opt/MVS/lib/CLProtocol')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

try:
    import cv2
    BAYER2BGR = {
        PixelType_Gvsp_BayerGR8: cv2.COLOR_BayerGB2BGR,
        PixelType_Gvsp_BayerGB8: cv2.COLOR_BayerGR2BGR,
        PixelType_Gvsp_BayerRG8: cv2.COLOR_BayerBG2BGR,
        PixelType_Gvsp_BayerBG8: cv2.COLOR_BayerRG2BGR,
    }
except ImportError:
    cv2 = None
    BAYER2BGR = {}


def get_float(cam, name):
    st = MVCC_FLOATVALUE()
    ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_FLOATVALUE))
    if cam.MV_CC_GetFloatValue(name, st) == 0:
        return st.fCurValue
    return float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=5.0)
    ap.add_argument('--out', default='hk_jetson_frame.png')
    args = ap.parse_args()

    dev_list = MV_CC_DEVICE_INFO_LIST()
    ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dev_list)
    if ret != 0 or dev_list.nDeviceNum == 0:
        print('未发现相机 (ret=%#x)' % ret)
        return 1

    dev_info = None
    for i in range(dev_list.nDeviceNum):
        info = dev_list.pDeviceInfo[i].contents
        if info.nTLayerType == MV_USB_DEVICE:
            dev_info = info
            usb = info.SpecialInfo.stUsb3VInfo
            model = bytes(usb.chModelName).split(b'\x00')[0].decode(errors='ignore')
            sn = bytes(usb.chSerialNumber).split(b'\x00')[0].decode(errors='ignore')
            print('设备: %s  SN=%s' % (model, sn))
            break
    if dev_info is None:
        print('没有 USB 相机（仅发现 GigE）')
        return 1

    cam = MvCamera()
    cam.MV_CC_CreateHandle(dev_info)
    cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    grabbed = False
    try:
        cam.MV_CC_SetEnumValue('TriggerMode', MV_TRIGGER_MODE_OFF)
        pixel = PixelType_Gvsp_BayerGR8
        for pt in (PixelType_Gvsp_BayerGR8, PixelType_Gvsp_BayerGB8,
                   PixelType_Gvsp_BayerRG8, PixelType_Gvsp_BayerBG8):
            if cam.MV_CC_SetEnumValue('PixelFormat', pt) == 0:
                pixel = pt
                break
        cam.MV_CC_SetEnumValue('ExposureAuto', 2)
        cam.MV_CC_SetEnumValue('GainAuto', 2)
        cam.MV_CC_SetBoolValue('AcquisitionFrameRateEnable', True)
        cam.MV_CC_SetFloatValue('AcquisitionFrameRate', 60.0)

        st = MVCC_INTVALUE_EX()
        ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_INTVALUE_EX))
        cam.MV_CC_GetIntValueEx('PayloadSize', st)
        payload = st.nCurValue
        buf = (ctypes.c_ubyte * payload)()
        info = MV_FRAME_OUT_INFO_EX()

        cam.MV_CC_StartGrabbing()
        grabbed = True
        print('PayloadSize=%d  预热 1.5s（AE 收敛）...' % payload)
        t_end = time.perf_counter() + 1.5
        while time.perf_counter() < t_end:
            cam.MV_CC_GetOneFrameTimeout(ctypes.byref(buf), payload, info, 500)

        n = 0
        fails = 0
        first = None
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < args.seconds:
            ret = cam.MV_CC_GetOneFrameTimeout(
                ctypes.byref(buf), payload, info, 500)
            if ret != 0:
                fails += 1
                continue
            n += 1
            if first is None:
                first = (info.nWidth, info.nHeight,
                         bytes(buf[:info.nFrameLen]))
        dt = time.perf_counter() - t0
        print('实测: %d 帧 / %.2fs = %.1f FPS (超时 %d 次)'
              % (n, dt, n / dt, fails))
        print('相机内部 ResultingFrameRate = %.2f'
              % get_float(cam, 'ResultingFrameRate'))
        print('提示: 两者都低→相机/曝光问题; 前者高后者低→USB 传输层问题')

        if first and cv2 is not None and pixel in BAYER2BGR:
            w, h, raw = first
            img = np.frombuffer(raw[:w * h], np.uint8).reshape(h, w)
            bgr = cv2.cvtColor(img, BAYER2BGR[pixel])
            cv2.imwrite(args.out, bgr)
            print('首帧已存: %s（正确 Bayer 相位）'
                  % os.path.abspath(args.out))
    finally:
        if grabbed:
            cam.MV_CC_StopGrabbing()
        cam.MV_CC_CloseDevice()
        cam.MV_CC_DestroyHandle()
        print('设备已安全释放')
    return 0


if __name__ == '__main__':
    sys.exit(main())
