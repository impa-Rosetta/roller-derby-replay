#!/usr/bin/env python3
"""Hikrobot MV-CS050-60UC 实时预览（MVS SDK）

枚举→打开→AE 自动曝光→实时显示，叠加 FPS / 曝光 / 增益 / 亮度。
RGB8 取流，缩放后转 BGR 显示（先缩放再转换，省 CPU）。

窗口按键:
  q / ESC  退出（优雅关闭）
  s        存当前帧到脚本目录 hk_preview_时间戳.png

用法:
  export LD_LIBRARY_PATH=/opt/MVS/lib/aarch64:$LD_LIBRARY_PATH
  python3 /home/ifit/xjh/hk_preview.py
"""
import ctypes
import os
import signal
import sys
import time

import cv2
import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_SOFTWARE_LIBENV', '/opt/MVS/lib')
os.environ.setdefault('MVCAM_GENICAM_CLPROTOCOL', '/opt/MVS/lib/CLProtocol')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

WIN = "MV-CS050-60UC preview  [q]uit  [s]ave"
WIN_W = 1280
SAVE_DIR = os.path.dirname(os.path.abspath(__file__))


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def open_camera():
    dev_list = MV_CC_DEVICE_INFO_LIST()
    ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dev_list)
    if ret != 0:
        raise RuntimeError('EnumDevices ret=%#x' % ret)

    dev_info = None
    for i in range(dev_list.nDeviceNum):
        info = dev_list.pDeviceInfo[i].contents
        if info.nTLayerType == MV_USB_DEVICE:
            dev_info = info
            break
    if dev_info is None:
        raise RuntimeError('未找到 USB3 相机')

    cam = MvCamera()
    ret = cam.MV_CC_CreateHandle(dev_info)
    if ret != 0:
        raise RuntimeError('CreateHandle ret=%#x' % ret)
    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        cam.MV_CC_DestroyHandle()
        raise RuntimeError('OpenDevice ret=%#x' % ret)

    cam.MV_CC_SetEnumValue('TriggerMode', MV_TRIGGER_MODE_OFF)
    ret = cam.MV_CC_SetEnumValue('PixelFormat', PixelType_Gvsp_RGB8_Packed)
    if ret != 0:
        raise RuntimeError('PixelFormat=RGB8 ret=%#x' % ret)
    # AE 连续自动曝光 + 自动增益，暗光环境下自动提亮
    cam.MV_CC_SetEnumValue('ExposureAuto', 2)
    cam.MV_CC_SetEnumValue('GainAuto', 2)

    return cam


def query_params(cam):
    exp = gain = 0.0
    st = MVCC_FLOATVALUE()
    ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_FLOATVALUE))
    if cam.MV_CC_GetFloatValue('ExposureTime', st) == 0:
        exp = st.fCurValue
    ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_FLOATVALUE))
    if cam.MV_CC_GetFloatValue('Gain', st) == 0:
        gain = st.fCurValue
    return exp, gain


def main():
    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    grabbed = False
    try:
        cam = open_camera()
        cam.MV_CC_StartGrabbing()
        grabbed = True
        print('AE 收敛中（约 2 秒）...')

        st = MVCC_INTVALUE_EX()
        ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_INTVALUE_EX))
        cam.MV_CC_GetIntValueEx('PayloadSize', st)
        payload = st.nCurValue
        data_buf = (ctypes.c_ubyte * payload)()
        frame_info = MV_FRAME_OUT_INFO_EX()

        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WIN, WIN_W, WIN_W * 2048 // 2448)

        t0, n, fps = time.time(), 0, 0.0
        exp, gain = query_params(cam)
        t_param = time.time()

        while True:
            ret = cam.MV_CC_GetOneFrameTimeout(
                ctypes.byref(data_buf), payload, frame_info, 200)
            if ret != 0:
                continue
            n += 1
            now = time.time()
            if now - t0 >= 1.0:
                fps = n / (now - t0)
                t0, n = now, 0
            if now - t_param >= 1.0:
                exp, gain = query_params(cam)
                t_param = now

            w, h = frame_info.nWidth, frame_info.nHeight
            arr = np.frombuffer(data_buf, np.uint8, frame_info.nFrameLen)
            img = arr.reshape(h, w, 3)  # RGB8
            disp = cv2.resize(img, (WIN_W, WIN_W * h // w),
                              interpolation=cv2.INTER_AREA)
            disp = cv2.cvtColor(disp, cv2.COLOR_RGB2BGR)

            text = ('%.1f FPS  exp=%.1fms  gain=%.1f  mean=%.1f'
                    % (fps, exp / 1000, gain, disp.mean()))
            cv2.putText(disp, text, (12, 36), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow(WIN, disp)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            try:  # 点窗口 × 时也优雅退出（防进程滞留取流）
                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    print('窗口已关闭，优雅退出')
                    break
            except cv2.error:
                break
            if key == ord('s'):
                fn = os.path.join(SAVE_DIR,
                                  time.strftime('hk_preview_%H%M%S.png'))
                cv2.imwrite(fn, disp)
                print('已存', fn)
    except KeyboardInterrupt:
        print('收到中断信号（Ctrl+C/SIGTERM）')
        return 130
    except Exception as e:
        print('错误: %s: %s' % (type(e).__name__, e))
        return 3
    finally:
        # 先释放相机（防非优雅断开导致 xHCI 假死），再销毁窗口
        if cam is not None:
            try:
                if grabbed:
                    cam.MV_CC_StopGrabbing()
                cam.MV_CC_CloseDevice()
                cam.MV_CC_DestroyHandle()
            except BaseException as e:
                print('关闭清理异常(忽略):', e)
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        print('已安全退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
