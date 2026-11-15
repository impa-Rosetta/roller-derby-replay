#!/usr/bin/env python3
"""Hikrobot MV-CS050-60UC 录制 10 秒 → 落盘 MJPG-AVI → 满帧率回放

Bayer8 满帧率录制（原始帧+时间戳缓存内存，约 3GB/10s），录完先释放
相机，再按实测帧率写出 MJPG-AVI（与 mv_camera_record.py 一致），
最后从文件按原生帧率回放。

窗口按键: q / ESC  退出回放

用法:
  export LD_LIBRARY_PATH=/opt/MVS/lib/aarch64:$LD_LIBRARY_PATH
  /usr/bin/python3 /home/ifit/xjh/hk_record_playback.py [--seconds 10] [--out 路径.avi]
"""
import argparse
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

WIN_W = 1280
WIN = "MV-CS050-60UC playback  [q]uit"
SAVE_DIR = os.path.dirname(os.path.abspath(__file__))

# 实测(2026-10-20，与 ISP RGB8 逐通道相关性分析)：海康 GenICam Bayer 命名
# 与 OpenCV cvtColor 命名相差一个相位，BayerGR8 必须用 COLOR_BayerGB 解析，
# 否则红蓝对调（中性区域不明显，皮肤会变蓝）
BAYER2BGR = {
    PixelType_Gvsp_BayerGR8: cv2.COLOR_BayerGB2BGR,
    PixelType_Gvsp_BayerGB8: cv2.COLOR_BayerGR2BGR,
    PixelType_Gvsp_BayerRG8: cv2.COLOR_BayerBG2BGR,
    PixelType_Gvsp_BayerBG8: cv2.COLOR_BayerRG2BGR,
}


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
    pixel = None
    for pt in (PixelType_Gvsp_BayerRG8, PixelType_Gvsp_BayerGB8,
               PixelType_Gvsp_BayerGR8, PixelType_Gvsp_BayerBG8):
        if cam.MV_CC_SetEnumValue('PixelFormat', pt) == 0:
            pixel = pt
            break
    if pixel is None:
        raise RuntimeError('相机不支持 Bayer8 输出')
    print('PixelFormat=0x%x (Bayer8)' % pixel)

    # AE 自动曝光，限制上限保证 60fps；帧率固定 60
    cam.MV_CC_SetFloatValue('AutoExposureTimeLowerLimit', 100.0)
    cam.MV_CC_SetFloatValue('AutoExposureTimeUpperLimit', 15000.0)
    cam.MV_CC_SetEnumValue('ExposureAuto', 2)
    cam.MV_CC_SetEnumValue('GainAuto', 2)
    # 连续自动白平衡（Bayer 外部转换绕过 ISP，无白平衡增益会偏蓝）
    cam.MV_CC_SetEnumValue('BalanceWhiteAuto', 2)
    cam.MV_CC_SetBoolValue('AcquisitionFrameRateEnable', True)
    cam.MV_CC_SetFloatValue('AcquisitionFrameRate', 60.0)

    return cam, pixel


def record(cam, seconds):
    """全速取流缓存原始 Bayer 帧，返回 (frames, w, h, 实测时长)"""
    st = MVCC_INTVALUE_EX()
    ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(MVCC_INTVALUE_EX))
    cam.MV_CC_GetIntValueEx('PayloadSize', st)
    payload = st.nCurValue
    buf = (ctypes.c_ubyte * payload)()
    info = MV_FRAME_OUT_INFO_EX()

    # AE/白平衡收敛：先丢弃 1.5 秒的帧，避免录进开头偏色
    t_warm = time.perf_counter()
    while time.perf_counter() - t_warm < 1.5:
        cam.MV_CC_GetOneFrameTimeout(ctypes.byref(buf), payload, info, 200)

    print('录制 %.0f 秒（AE/WB 已收敛，无窗口全速）...' % seconds)
    frames, w, h = [], 0, 0
    t_start = time.perf_counter()
    last = t_start
    while True:
        now = time.perf_counter()
        if now - t_start >= seconds:
            break
        ret = cam.MV_CC_GetOneFrameTimeout(
            ctypes.byref(buf), payload, info, 1000)
        if ret != 0:
            continue
        frames.append(np.frombuffer(buf, np.uint8, info.nFrameLen).copy())
        w, h = info.nWidth, info.nHeight
        if now - last >= 2.0:
            print('  %.1fs  已缓存 %d 帧 (%.1f FPS)'
                  % (now - t_start, len(frames),
                     len(frames) / (now - t_start)))
            last = now
    dur = time.perf_counter() - t_start
    print('录制结束: %d 帧 / %.2f 秒 = %.1f FPS'
          % (len(frames), dur, len(frames) / dur))
    return frames, w, h, dur


def write_video(frames, w, h, fps, out, code):
    """Bayer→BGR 后按实测帧率写 MJPG-AVI"""
    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*'MJPG'),
                             fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError('无法创建视频文件: %s' % out)
    try:
        for i, data in enumerate(frames):
            bgr = cv2.cvtColor(data.reshape(h, w), code)
            writer.write(bgr)
            if (i + 1) % 120 == 0:
                print('  写盘 %d/%d ...' % (i + 1, len(frames)))
    finally:
        writer.release()


def playback(path, win_w):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError('无法打开视频: %s' % path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print('回放 %s (%dx%d @ %.1f FPS, %d 帧)'
          % (os.path.basename(path), w, h, fps, n_total))

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, win_w, win_w * h // w)

    t0 = time.perf_counter()
    i = 0
    shown, t_prev, shown_fps = 0, t0, 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        # 按原生帧率时间轴驱动（解码慢时自然降速，不跳帧）
        target = t0 + i / fps
        wait = target - time.perf_counter()
        if wait > 0:
            time.sleep(wait)

        disp = cv2.resize(frame, (win_w, win_w * h // w),
                          interpolation=cv2.INTER_LINEAR)
        shown += 1
        now = time.perf_counter()
        if now - t_prev >= 0.5:
            shown_fps = shown / (now - t_prev)
            shown = 0
            t_prev = now
        text = 'frame %d/%d  play %.1f FPS' % (i, n_total, shown_fps)
        cv2.putText(disp, text, (12, 36), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.imshow(WIN, disp)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):
            print('回放被手动中断')
            break
        try:
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                print('窗口已关闭')
                break
        except cv2.error:
            break

    total = time.perf_counter() - t0
    print('回放结束: %d 帧 / %.2f 秒 = %.1f FPS'
          % (min(i, n_total), total, min(i, n_total) / total))
    cap.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=10.0,
                    help='录制时长秒（默认 10）')
    ap.add_argument('--out', default='',
                    help='输出 .avi 路径（默认 脚本目录/record_hk_时间戳.avi）')
    ap.add_argument('--no-playback', action='store_true',
                    help='录完不回放（Jetson/无显示环境用）')
    args = ap.parse_args()

    out = args.out or os.path.join(
        SAVE_DIR, time.strftime('record_hk_%Y%m%d_%H%M%S.avi'))

    signal.signal(signal.SIGTERM, _on_term)

    cam = None
    grabbed = False
    try:
        cam, pixel = open_camera()
        cam.MV_CC_StartGrabbing()
        grabbed = True

        frames, w, h, dur = record(cam, args.seconds)

        # 先释放相机（防非优雅断开导致 xHCI 假死）
        cam.MV_CC_StopGrabbing()
        grabbed = False
        cam.MV_CC_CloseDevice()
        cam.MV_CC_DestroyHandle()
        cam = None
        print('相机已释放，开始写盘')

        if not frames:
            print('没有录到帧')
            return 1

        code = BAYER2BGR.get(pixel, cv2.COLOR_BayerGR2BGR)
        fps = len(frames) / dur if dur > 0 else 60.0
        t_w = time.perf_counter()
        write_video(frames, w, h, fps, out, code)
        print('已存: %s  (%d 帧 @ %.1f FPS, %.1f MB, 转码 %.1fs)'
              % (out, len(frames), fps, os.path.getsize(out) / 1e6,
                 time.perf_counter() - t_w))
        del frames  # 释放约 3GB 内存

        if args.no_playback:
            print('跳过回放（--no-playback）')
        else:
            playback(out, WIN_W)
    except KeyboardInterrupt:
        print('收到中断信号（Ctrl+C/SIGTERM）')
        return 130
    except Exception as e:
        print('错误: %s: %s' % (type(e).__name__, e))
        return 3
    finally:
        # 先释放相机，再销毁窗口
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
