#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段1基准：NPP GPU 去马赛克 + RGB→I420，单帧耗时与 Bayer 相位校验

流程：
  1. 开相机取 1 帧 BayerGR8（取完立即优雅关闭）
  2. 用 NPP 四种 Bayer 相位各转一遍，与项目现有 OpenCV 参考结果比对，
     自动判定正确相位（GenICam 与 OpenCV 命名差一个相位，NPP 可能也有偏差）
  3. 计时：仅去马赛克 / 去马赛克+RGB→I420，单帧延迟与批量吞吐
  4. 存 npp_rgb.jpg / cv_ref.jpg 供人眼复核

用法（Jetson）:
  source /etc/profile.d/mvs_env.sh
  /usr/bin/python3 ~/hk_camera/hk_npp_bench.py
"""
import ctypes as C
import os
import sys
import time

import numpy as np

os.environ.setdefault('MVCAM_SDK_PATH', '/opt/MVS')
os.environ.setdefault('MVCAM_COMMON_RUNENV', '/opt/MVS/lib')
sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')

from MvCameraControl_class import *  # noqa: E402

import cv2  # noqa: E402

CUDA_LIB = '/usr/local/cuda/lib64'
os.environ.setdefault('LD_LIBRARY_PATH', CUDA_LIB)

# NPP 枚举（nppdefs.h）：BGGR=0, RGGB=1, GBRG=2, GRBG=3
NPP_GRIDS = {0: 'BGGR', 1: 'RGGB', 2: 'GBRG', 3: 'GRBG'}
OPAQUE = 0  # NPPI_INTER_UNDEFINED，CFA 转换唯一允许值


class NppiSize(C.Structure):
    _fields_ = [('width', C.c_int), ('height', C.c_int)]


class NppiRect(C.Structure):
    _fields_ = [('x', C.c_int), ('y', C.c_int),
                ('width', C.c_int), ('height', C.c_int)]


def load_libs():
    rt = C.CDLL(os.path.join(CUDA_LIB, 'libcudart.so.12'))
    npp = C.CDLL(os.path.join(CUDA_LIB, 'libnppicc.so.12'))
    npp.nppiCFAToRGB_8u_C1C3R.argtypes = [
        C.c_void_p, C.c_int, NppiSize, NppiRect,
        C.c_void_p, C.c_int, C.c_int, C.c_int]
    npp.nppiCFAToRGB_8u_C1C3R.restype = C.c_int
    npp.nppiRGBToYUV420_8u_C3P3R.argtypes = [
        C.c_void_p, C.c_int, C.POINTER(C.c_void_p),
        C.POINTER(C.c_int), NppiSize]
    npp.nppiRGBToYUV420_8u_C3P3R.restype = C.c_int
    rt.cudaHostRegister.argtypes = [C.c_void_p, C.c_size_t, C.c_uint]
    rt.cudaHostGetDevicePointer.argtypes = [
        C.POINTER(C.c_void_p), C.c_void_p, C.c_uint]
    rt.cudaDeviceSynchronize.restype = C.c_int
    rt.cudaHostUnregister.argtypes = [C.c_void_p]
    return rt, npp


def register(rt, arr):
    """把 numpy 内存注册为 CUDA 映射内存，返回设备指针（Tegra 上通常等于主机指针）"""
    addr = arr.ctypes.data
    rc = rt.cudaHostRegister(C.c_void_p(addr), arr.nbytes, 2)  # Mapped
    if rc != 0:
        raise RuntimeError('cudaHostRegister rc=%d size=%.1fMB'
                           % (rc, arr.nbytes / 1e6))
    p = C.c_void_p()
    rc = rt.cudaHostGetDevicePointer(C.byref(p), C.c_void_p(addr), 0)
    if rc != 0:
        raise RuntimeError('cudaHostGetDevicePointer rc=%d' % rc)
    return p.value or addr


def grab_one_bayer():
    """开相机 → 取 3 帧（等 AE 稳定）→ 优雅关闭，返回 (帧, w, h)"""
    dev_list = MV_CC_DEVICE_INFO_LIST()
    MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dev_list)
    dev_info = None
    for i in range(dev_list.nDeviceNum):
        info = dev_list.pDeviceInfo[i].contents
        if info.nTLayerType == MV_USB_DEVICE:
            dev_info = info
            break
    if dev_info is None:
        raise RuntimeError('没有 USB 相机')

    cam = MvCamera()
    cam.MV_CC_CreateHandle(dev_info)
    cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    grabbed = False
    try:
        cam.MV_CC_SetEnumValue('TriggerMode', MV_TRIGGER_MODE_OFF)
        cam.MV_CC_SetEnumValue('PixelFormat', PixelType_Gvsp_BayerGR8)
        cam.MV_CC_SetEnumValue('ExposureAuto', 2)
        cam.MV_CC_SetEnumValue('GainAuto', 2)
        cam.MV_CC_SetBoolValue('AcquisitionFrameRateEnable', True)
        cam.MV_CC_SetFloatValue('AcquisitionFrameRate', 60.0)
        st = MVCC_INTVALUE_EX()
        C.memset(C.byref(st), 0, C.sizeof(MVCC_INTVALUE_EX))
        cam.MV_CC_GetIntValueEx('PayloadSize', st)
        payload = st.nCurValue
        buf = (C.c_ubyte * payload)()
        info = MV_FRAME_OUT_INFO_EX()
        cam.MV_CC_StartGrabbing()
        grabbed = True
        frame = None
        for _ in range(3):
            if cam.MV_CC_GetOneFrameTimeout(C.byref(buf), payload, info, 1000) == 0:
                frame = np.frombuffer(buf, np.uint8, info.nFrameLen).copy()
                w, h = info.nWidth, info.nHeight
        if frame is None:
            raise RuntimeError('取帧失败')
        return frame, w, h
    finally:
        if grabbed:
            cam.MV_CC_StopGrabbing()
        cam.MV_CC_CloseDevice()
        cam.MV_CC_DestroyHandle()


def main():
    rt, npp = load_libs()

    t_cam = time.perf_counter()
    bayer, w, h = grab_one_bayer()
    print('取帧: %dx%d %.2f MB (%.1fs)'
          % (w, h, bayer.size / 1e6, time.perf_counter() - t_cam))
    print('帧长 %d，与 w*h 一致: %s' % (bayer.size, bayer.size == w * h))

    src = bayer.reshape(h, w)
    p_src = register(rt, src)

    rgb = np.empty((h, w, 3), np.uint8)
    p_rgb = register(rt, rgb)

    y = np.empty((h, w), np.uint8)
    u = np.empty((h // 2, w // 2), np.uint8)
    v = np.empty((h // 2, w // 2), np.uint8)
    p_y, p_u, p_v = register(rt, y), register(rt, u), register(rt, v)

    size = NppiSize(w, h)
    rect = NppiRect(0, 0, w, h)
    dst3 = (C.c_void_p * 3)(p_y, p_u, p_v)
    step3 = (C.c_int * 3)(w, w // 2, w // 2)

    # ---- 1. 相位判定：与项目现有 OpenCV 参考（BayerGR8→COLOR_BayerGB）比对 ----
    ref_bgr = cv2.cvtColor(src, cv2.COLOR_BayerGB2BGR)
    cv2.imwrite('/tmp/cv_ref.jpg', ref_bgr)
    ref = ref_bgr.astype(np.int16)

    print('\nNPP 相位比对（与 OpenCV 参考的平均绝对差，越小越像）:')
    best, best_diff = None, 1e18
    for g in range(4):
        rc = npp.nppiCFAToRGB_8u_C1C3R(
            p_src, w, size, rect, p_rgb, w * 3, g, OPAQUE)
        rt.cudaDeviceSynchronize()
        if rc != 0:
            print('  %-4s NPP rc=%d' % (NPP_GRIDS[g], rc))
            continue
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).astype(np.int16)
        diff = float(np.abs(bgr - ref).mean())
        print('  %-4s 平均差 %.2f' % (NPP_GRIDS[g], diff))
        if diff < best_diff:
            best, best_diff, best_img = g, diff, bgr.astype(np.uint8)
    if best is None:
        raise RuntimeError('四种相位全部失败')
    cv2.imwrite('/tmp/npp_rgb.jpg', best_img)
    print('判定：NPP 使用 %s（差 %.2f），已存 /tmp/npp_rgb.jpg'
          % (NPP_GRIDS[best], best_diff))

    # ---- 2. 计时 ----
    def loop(n, with_yuv):
        t0 = time.perf_counter()
        for _ in range(n):
            npp.nppiCFAToRGB_8u_C1C3R(
                p_src, w, size, rect, p_rgb, w * 3, best, OPAQUE)
            if with_yuv:
                npp.nppiRGBToYUV420_8u_C3P3R(p_rgb, w * 3, dst3, step3, size)
            rt.cudaDeviceSynchronize()
        return (time.perf_counter() - t0) / n * 1000.0

    for _ in range(10):
        npp.nppiCFAToRGB_8u_C1C3R(p_src, w, size, rect, p_rgb, w * 3, best, OPAQUE)
    rt.cudaDeviceSynchronize()

    ms_dm = loop(60, False)
    ms_full = loop(60, True)
    print('\n单帧延迟（含同步）:')
    print('  去马赛克          %6.2f ms  (%.1f fps 上限)'
          % (ms_dm, 1000.0 / ms_dm))
    print('  去马赛克+RGB→I420 %6.2f ms  (%.1f fps 上限)'
          % (ms_full, 1000.0 / ms_full))
    print('  60fps 预算 16.67ms，剩余 %.2f ms'
          % (16.67 - ms_full))

    # ---- 3. NPP 产出的 I420 颜色/范围自检 ----
    i420 = np.concatenate([y.ravel(), u.ravel(), v.ravel()])
    back = cv2.cvtColor(i420.reshape(h * 3 // 2, w),
                        cv2.COLOR_YUV2BGR_I420)
    cv2.imwrite('/tmp/npp_yuv_roundtrip.jpg', back)
    ymean, ymax = float(y.mean()), int(y.max())
    print('\nY 通道: 均值 %.1f 最大 %d（>235 多则可能是全范围）'
          % (ymean, ymax))

    # 释放注册内存，避免影响后续进程
    for arr in (src, rgb, y, u, v):
        rt.cudaHostUnregister(C.c_void_p(arr.ctypes.data))
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
