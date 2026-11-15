#!/usr/bin/env python3
"""把高帧率录像转成通用慢动作 MP4（发给别人/手机直接可播）

源 164fps 素材按「30fps 容器 × 每帧重复 N 次」写出：有效播放帧率 =
30/N fps，慢放倍数 = 源帧率 / (30/N)。默认 N=3 → 有效 10fps →
16.4x 慢动作（与慢放播放器 --fps 10 观感一致），时长 = 源时长 × 16.4。
编码优先 H.264(avc1)，不可用退 MPEG-4(mp4v)；封装 MP4 通吃
微信/手机/任意播放器（对比 MJPG-AVI 手机放不了）。

用法:
  /usr/bin/python3 /home/ifit/xjh/mv_make_slowmo.py record_xxx.avi
  → 同目录 record_xxx_slowmo.mp4
"""
import argparse
import os
import sys
import time

import cv2

CONTAINER_FPS = 30  # 容器帧率（标准值，兼容性最好）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="源录像（如 record_20261014_111133.avi）")
    ap.add_argument("-o", "--out", default="",
                    help="输出 mp4（默认同目录 _slowmo.mp4）")
    ap.add_argument("--repeat", type=int, default=3,
                    help="每帧重复次数（30fps 容器；3=有效10fps=16.4x 慢放）")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.isfile(src):
        print("找不到源文件:", src)
        return 2
    out = args.out or os.path.splitext(src)[0] + "_slowmo.mp4"

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print("无法打开:", src)
        return 2
    n_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    play_fps = CONTAINER_FPS / args.repeat
    slow = (src_fps / play_fps) if src_fps > 0 else 0
    print(f"源: {n_src} 帧 @ {src_fps:.1f} FPS  {w}x{h}")
    print(f"出: 30fps 容器 x 每帧 {args.repeat} 次 -> 有效 {play_fps:.1f} FPS"
          f" = {slow:.1f}x 慢动作, 时长约 {n_src / play_fps:.0f}s")

    writer = None
    for cc in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*cc),
                                 CONTAINER_FPS, (w, h))
        if writer.isOpened():
            print("编码器:", cc)
            break
        writer.release()
        writer = None
    if writer is None:
        print("无可用编码器")
        cap.release()
        return 3

    t0 = time.time()
    n = 0
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            for _ in range(args.repeat):
                writer.write(f)
            n += 1
            if n % 200 == 0:
                el = time.time() - t0
                print(f"  {n}/{n_src} 源帧  {el:.0f}s  "
                      f"({n / el:.1f} 源帧/秒)", flush=True)
    finally:
        writer.release()
        cap.release()

    el = time.time() - t0
    got = n * args.repeat
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        print(f"完成: {out}")
        print(f"  {got} 输出帧（{n} 源帧 x{args.repeat}）/ 耗时 {el:.0f}s / "
              f"体积 {os.path.getsize(out) / 1e6:.1f} MB / "
              f"播放时长 {got / CONTAINER_FPS:.0f}s")
    else:
        print("写文件失败")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
