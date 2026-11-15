#!/usr/bin/env python3
"""高帧率视频慢动作回放器（OpenCV，无需 ffmpeg）

播放 mv_camera_record.py 录制的高帧率 MJPG-AVI：默认以 30fps 逐帧播放，
源 165fps 时即 5.5 倍慢动作。本机解码实测 253fps，逐帧解码无瓶颈。

按键:
  空格       暂停/继续
  [ / ]      播放帧率 -/+（慢动作倍率 = 源帧率/播放帧率）
  , / .      单帧后退/前进（自动暂停）
  ← / →      源时间 -/+ 1 秒
  l          循环开关
  q / ESC    退出

用法:
  /usr/bin/python3 /home/ifit/xjh/mv_slowmo_play.py 视频.avi [--fps 30] [--src-fps 0]
"""
import argparse
import signal
import sys
import time

import cv2

WIN = "slow-mo player  [space]pause  [ [ ]speed  [,/.]step  [l]oop  [q]uit"


def _on_term(_sig, _frame):
    raise KeyboardInterrupt


def put(img, text, y, color=(0, 255, 0)):
    cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color,
                2, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="高帧率 MJPG-AVI 路径")
    ap.add_argument("--fps", type=float, default=30.0,
                    help="播放帧率（默认 30 → 165fps 源即 5.5x 慢动作）")
    ap.add_argument("--src-fps", type=float, default=0.0,
                    help="源帧率覆盖（0=读容器标记）")
    ap.add_argument("--loop", action="store_true", help="启动即循环播放")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_term)

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print("打不开视频:", args.video, flush=True)
        return 3
    src_fps = args.src_fps or cap.get(cv2.CAP_PROP_FPS)
    if not 1.0 <= src_fps <= 2000.0:
        print(f"容器帧率异常({src_fps})，回退 30fps；可用 --src-fps 指定",
              flush=True)
        src_fps = 30.0
    total = max(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 1)
    play_fps = max(1.0, min(args.fps, src_fps))
    print(f"{args.video}: {total} 帧  源 {src_fps:.2f} FPS  "
          f"时长 {total / src_fps:.2f}s  →  {play_fps:.0f} FPS 播放 "
          f"（{src_fps / play_fps:.1f}x 慢动作）", flush=True)

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1280, 800)

    idx = 0          # 下一帧索引
    shown = -1       # 当前显示帧索引
    paused = False
    looping = args.loop
    pending = True   # 强制立即取帧
    frame = None
    next_t = time.perf_counter()

    try:
        while True:
            now = time.perf_counter()
            if pending or (not paused and now >= next_t):
                ok, f = cap.read()
                if not ok:
                    if looping:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        idx = 0
                        ok, f = cap.read()
                    if not ok:
                        print("播放结束", flush=True)
                        break
                frame = f
                shown = idx
                idx += 1
                pending = False
                if not paused:
                    next_t += 1.0 / play_fps
                    if next_t < now:           # 落后过多 → 重置节拍防追帧
                        next_t = now + 1.0 / play_fps
                t_src = shown / src_fps
                put(frame, f"{t_src:6.2f}s  frame {shown}/{total}  "
                           f"{src_fps:.0f}->{play_fps:.0f} fps  "
                           f"{src_fps / play_fps:.1f}x slow", 40)
                if paused:
                    put(frame, "PAUSED", 92, (0, 255, 255))
                cv2.imshow(WIN, frame)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord('q'), 27):
                break
            elif k == ord(' '):
                paused = not paused
                if not paused:
                    next_t = time.perf_counter()
            elif k == ord('['):
                play_fps = max(1.0, play_fps / 1.25)
                next_t = time.perf_counter()
            elif k == ord(']'):
                play_fps = min(src_fps, play_fps * 1.25)
                next_t = time.perf_counter()
            elif k == ord(','):
                paused = True
                idx = max(0, shown - 1)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                pending = True
            elif k == ord('.'):
                paused = True
                idx = min(total - 1, shown + 1)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                pending = True
            elif k == 81:  # ← 后退 1 秒（源时间）
                idx = max(0, shown - int(src_fps))
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                next_t = time.perf_counter()
                pending = True
            elif k == 83:  # → 前进 1 秒
                idx = min(total - 1, shown + int(src_fps))
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                next_t = time.perf_counter()
                pending = True
            elif k == ord('l'):
                looping = not looping
                print("循环:", "开" if looping else "关", flush=True)
    except KeyboardInterrupt:
        print("收到中断信号", flush=True)
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print("已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
