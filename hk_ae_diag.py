#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海康相机 AE/曝光节点诊断：查清 0x80000109 到底是"节点不存在"还是别的。

背景：管线里 MV_CC_SetEnumValue('ExposureAuto', 2) 永久失败，
返回 0x80000109。查 /opt/MVS/include/MvErrorDefine.h 确认：
    0x80000109 = MV_E_GC_NODE_NOT_FOUND  （节点不存在，不是"不支持"）
也就是说相机 XML 里根本没有 ExposureAuto 这个节点名。本脚本枚举真相机
实际暴露的全部节点名，找出这台相机真正该用的曝光/自动曝光节点。

用法（在 Jetson 上跑）：
    /usr/bin/python3 hk_ae_diag.py
    /usr/bin/python3 hk_ae_diag.py --grab     # 顺带验证取帧与帧率
"""
import argparse
import os
import sys
import time

sys.path.insert(0, '/opt/MVS/Samples/aarch64/Python/MvImport')
from MvCameraControl_class import *  # noqa: E402,F403

# MvErrorDefine.h 里与本问题相关的码
ERR = {
    0x80000109: 'MV_E_GC_NODE_NOT_FOUND（节点不存在）',
    0x80000106: 'MV_E_GC_ACCESS（节点访问条件不满足）',
    0x8000010A: 'MV_E_GC_NODE_VERIFY（节点校验失败）',
    0x80000202: 'MV_E_WRITE_PROTECT（目标地址不可写）',
    0x80000203: 'MV_E_ACCESS_DENIED（设备无访问权限）',
    0x80000007: 'MV_E_NODATA（超时无数据）',
    0x80000001: 'MV_E_SUPPORT（不支持的功能）',
}


def d(code):
    if code == 0:
        return 'OK'
    return '0x%08X %s' % (code & 0xFFFFFFFF, ERR.get(code, '(未收录)'))


def enum_devices():
    dev_list = MV_CC_DEVICE_INFO_LIST()
    tlayer = MV_GIGE_DEVICE | MV_USB_DEVICE
    ret = MvCamera.MV_CC_EnumDevices(tlayer, dev_list)
    if ret != 0:
        print('枚举失败:', d(ret))
        return None
    print('发现设备 %d 台' % dev_list.nDeviceNum)
    for i in range(dev_list.nDeviceNum):
        info = cast(dev_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        if info.nTLayerType == MV_USB_DEVICE:
            src = info.SpecialInfo.stUsb3VInfo
        else:
            src = info.SpecialInfo.stGigEInfo
        model = bytes(src.chModelName).split(b'\x00')[0].decode('utf-8', 'ignore')
        serial = bytes(src.chSerialNumber).split(b'\x00')[0].decode('utf-8', 'ignore')
        print('  [%d] %s  SN=%s' % (i, model, serial))
    return dev_list


def try_get(cam, kind, key):
    """尝试按 kind 读节点，返回 (ret, 值或 None)。"""
    if kind == 'float':
        st = MVCC_FLOATVALUE()
        memset(byref(st), 0, sizeof(st))
        r = cam.MV_CC_GetFloatValue(key, st)
        return r, (st.fCurValue if r == 0 else None)
    if kind == 'enum':
        st = MVCC_ENUMVALUE()
        memset(byref(st), 0, sizeof(st))
        r = cam.MV_CC_GetEnumValue(key, st)
        return r, (st.nCurValue if r == 0 else None)
    if kind == 'int':
        st = MVCC_INTVALUE_EX()
        memset(byref(st), 0, sizeof(st))
        r = cam.MV_CC_GetIntValueEx(key, st)
        return r, (st.nCurValue if r == 0 else None)
    if kind == 'bool':
        b = c_bool(False)
        r = cam.MV_CC_GetBoolValue(key, b)
        return r, (b.value if r == 0 else None)
    return -1, None


# 候选节点名：海康不同系列对同一功能的命名不一样，逐个探
CANDIDATES = [
    # 自动曝光主开关（GenICam 标准名是 ExposureAuto，海康部分型号叫别的）
    ('ExposureAuto', 'enum'),
    ('ExposureAutoMode', 'enum'),
    ('AutoExposure', 'enum'),
    ('ExposureMode', 'enum'),
    # 曝光时间主体
    ('ExposureTime', 'float'),
    ('ExposureTimeAbs', 'float'),
    # 自动曝光的上下限（可能是 INT 也可能是 FLOAT）
    ('AutoExposureTimeLowerLimit', 'int'),
    ('AutoExposureTimeLowerLimit', 'float'),
    ('AutoExposureTimeUpperLimit', 'int'),
    ('AutoExposureTimeUpperLimit', 'float'),
    ('ExposureAutoLowerLimit', 'float'),
    ('ExposureAutoUpperLimit', 'float'),
    # 增益自动
    ('GainAuto', 'enum'),
    ('Gain', 'float'),
    # 白平衡自动
    ('BalanceWhiteAuto', 'enum'),
    # 帧率
    ('AcquisitionFrameRate', 'float'),
    ('AcquisitionFrameRateEnable', 'bool'),
    ('ResultingFrameRate', 'float'),
]


def probe_nodes(cam):
    print('\n=== 节点探测（这是本诊断的核心）===')
    print('%-32s %-6s %s' % ('节点名', '类型', '结果'))
    print('-' * 78)
    found = {}
    for key, kind in CANDIDATES:
        r, v = try_get(cam, kind, key)
        mark = '✅' if r == 0 else '❌'
        print('%-32s %-6s %s %s' % (key, kind, mark, d(r)
                                    + ('' if v is None else '  值=%s' % v)))
        if r == 0:
            found.setdefault(key, {})[kind] = v
    return found


def try_official_paths(cam):
    """官方两条路：1) GenICam 节点  2) 老式专用接口（ctypes 直调）。"""
    print('\n=== 官方路径 2：老式专用接口 MV_CC_SetExposureAutoMode ===')
    lib = MvCamCtrldll
    try:
        lib.MV_CC_SetExposureAutoMode.argtype = (c_void_p, c_uint)
        lib.MV_CC_SetExposureAutoMode.restype = c_uint
        # 0=Off 1=Once 2=Continuous（CameraParams.h: MV_CAM_EXPOSURE_AUTO_MODE）
        for mode, name in ((2, 'Continuous'), (0, 'Off')):
            r = lib.MV_CC_SetExposureAutoMode(cam.handle, c_uint(mode))
            print('  SetExposureAutoMode(%s=%d) -> %s' % (name, mode, d(r)))
            if r != 0:
                break
    except AttributeError:
        print('  ❌ 库中没有 MV_CC_SetExposureAutoMode（该系列不支持）')


def grab_test(cam, seconds=3.0):
    print('\n=== 取帧测试 %.0fs ===' % seconds)
    st = MVCC_INTVALUE_EX()
    memset(byref(st), 0, sizeof(st))
    r = cam.MV_CC_GetIntValueEx('PayloadSize', st)
    if r != 0:
        print('  拿不到 PayloadSize:', d(r))
        return
    size = st.nCurValue
    buf = (c_ubyte * size)()
    stframe = MV_FRAME_OUT_INFO_EX()
    n = 0
    t0 = time.time()
    fails = {}
    while time.time() - t0 < seconds:
        memset(byref(stframe), 0, sizeof(stframe))
        r = cam.MV_CC_GetOneFrameTimeout(buf, size, stframe, 1000)
        if r == 0:
            n += 1
        else:
            fails[r] = fails.get(r, 0) + 1
    el = time.time() - t0
    print('  取到 %d 帧 / %.2fs = %.2f fps' % (n, el, n / el))
    for code, cnt in fails.items():
        print('  失败 %d 次: %s' % (cnt, d(code)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--grab', action='store_true', help='顺带做取帧测试')
    ap.add_argument('--index', type=int, default=0, help='设备序号')
    args = ap.parse_args()

    cam = MvCamera()
    dev_list = enum_devices()
    if not dev_list or dev_list.nDeviceNum == 0:
        print('没有相机')
        return 1
    if args.index >= dev_list.nDeviceNum:
        print('序号越界')
        return 1

    info = cast(dev_list.pDeviceInfo[args.index],
                POINTER(MV_CC_DEVICE_INFO)).contents
    r = cam.MV_CC_CreateHandle(info)
    print('\nCreateHandle:', d(r))
    if r != 0:
        return 1
    r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    print('OpenDevice:', d(r))
    if r != 0:
        cam.MV_CC_DestroyHandle()
        return 1

    try:
        # 先在不取流的状态下探测（GenICam 规定多数参数只能在停止取流时改）
        found = probe_nodes(cam)
        try_official_paths(cam)

        if args.grab:
            cam.MV_CC_SetEnumValue('TriggerMode', 0)
            st = MVCC_INTVALUE_EX()
            memset(byref(st), 0, sizeof(st))
            cam.MV_CC_GetIntValueEx('PayloadSize', st)
            size = st.nCurValue
            cam.MV_CC_SetImageNodeNum(5)
            r = cam.MV_CC_StartGrabbing()
            print('\nStartGrabbing:', d(r))
            if r == 0:
                grab_test(cam)
                ret = cam.MV_CC_StopGrabbing()
                print('StopGrabbing:', d(ret))
                # NODATA 的那套老坑：Stop → Start 必须隔 >=10ms
                time.sleep(0.05)

        print('\n=== 结论建议 ===')
        if 'ExposureAuto' in found:
            print('  GenICam 节点 ExposureAuto 存在，用 MV_CC_SetEnumValue 设置')
        elif 'ExposureAutoMode' in found or 'AutoExposure' in found:
            print('  ✅ 节点名不是 ExposureAuto，而是 %s —— 改管线里的节点名'
                  % [k for k in ('ExposureAutoMode', 'AutoExposure') if k in found])
        else:
            print('  ⚠️ 通用节点都没有，看上面老式接口 MV_CC_SetExposureAutoMode 的结果')
        if ('AutoExposureTimeUpperLimit' in found
                or 'ExposureAutoUpperLimit' in found):
            print('  自动曝光上限节点存在，注意它是 INT 还是 FLOAT（见上表）')
    finally:
        cam.MV_CC_CloseDevice()
        cam.MV_CC_DestroyHandle()
    return 0


if __name__ == '__main__':
    sys.exit(main())
