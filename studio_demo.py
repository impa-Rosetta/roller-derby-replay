#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛事监看台 · 离线演示（不连 Jetson，用本地缓存片段）

用于在没有网络 / 没有实时源时预览 UI 视觉与回放交互。
它构造一个与正式控制器接口一致的 DemoController，喂本地 mkv。

用法:
  /usr/bin/python3 studio_demo.py                 # 自动选本地缓存片段
  /usr/bin/python3 studio_demo.py --clip x.mkv    # 指定片段
"""
import argparse
import glob
import os
import sys
import time

import cv2

import studio_services as svc
from studio_services import fmt_time


class DemoArchive:
    """把本地片段伪装成 Archive（提供 snapshot / last_end 等接口）"""

    def __init__(self, segs):
        self._segs = segs
        self.error = ''

    def snapshot(self):
        return list(self._segs)

    def last_end(self):
        return self._segs[-1]['end_s'] if self._segs else 0.0

    def session_name(self):
        return 'demo'


class DemoLoader:
    """本地片段直接可用，不需要拉取"""

    def __init__(self, clips):
        self._clips = clips  # name -> info

    def clip(self, name):
        return self._clips.get(name)

    def error(self, name):
        return None

    def loading_name(self):
        return ''

    def request(self, seg, idle_only=False):
        pass


class DemoController:
    """与 StudioController 接口兼容，只从本地片段回放"""

    def __init__(self, args, clips):
        self.args = args
        segs = []
        t = 0.0
        for name, path in clips:
            cap = cv2.VideoCapture(path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 59.94
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            cap.release()
            dur = n / fps
            segs.append({'file': name, 'dir': '', 'frames': n,
                         'start_ms': t * 1000, 'end_ms': (t + dur) * 1000,
                         'start_s': t, 'end_s': t + dur})
            t += dur
        self.archive = DemoArchive(segs)
        self.loader = DemoLoader({name: {'mkv': path, 'fps': segs[i]['frames'] /
                                         (segs[i]['end_s'] - segs[i]['start_s']),
                                         'start_s': segs[i]['start_s'],
                                         'frames': segs[i]['frames']}
                                  for i, (name, path) in enumerate(clips)})

        self.mode = 'replay'
        self.speed_idx = 0
        self.paused = False
        self.ended = False
        self.remote_busy = False
        self.recording = False
        self.stop_confirm_t = 0.0
        self.clock_t0 = time.monotonic()
        self.clock_offset = 0.0
        self.msg = '离线演示模式：本地片段回放'
        self.msg_t = time.monotonic()

        self.cap = None
        self.seg = None
        self.fps = 59.94
        self.frame_count = 0
        self.idx = 0
        self.pos_s = 0.0
        self.pending = None
        self.drawn = None
        self.last_frame_obj = None
        self._marks = []
        self._mark_seq = 0
        self._quit = False

        # 打开第一段
        if segs:
            info = self.loader.clip(segs[0]['file'])
            self._open_seg(segs[0], info, 0.0)

    # ---- 兼容接口 ----
    def start(self):
        pass

    def request_quit(self):
        self._quit = True

    def shutdown(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

    def quit_requested(self):
        return self._quit

    def state(self):
        now = time.monotonic()
        clk = self.clock_offset + (now - self.clock_t0)
        return {
            'live_ok': False,
            'live_connecting': False,
            'live_fps': 0.0,
            'recording': False,
            'remote_busy': False,
            'mode': self.mode,
            'playing': (self.cap is not None and not self.paused
                        and not self.ended),
            'speed_idx': self.speed_idx,
            'clock': fmt_time(clk),
            'session': '演示场次',
            'session_len': fmt_time(self.archive.last_end()),
            'status': '离线演示 · %d 个本地片段' % len(self.archive.snapshot()),
        }

    def current_frame(self):
        self.last_frame_obj = self.drawn
        return self.drawn

    def live_frame(self):
        return None

    def live_fps(self):
        return 0.0

    def placeholder_text(self):
        return '演示：无画面'

    def timeline_end(self):
        return self.archive.last_end()

    def segments(self):
        return self.archive.snapshot()

    def segments_window(self, span_s=600.0):
        segs = self.archive.snapshot()
        if not segs:
            return segs, 0.0
        end = segs[-1]['end_s']
        t0 = max(0.0, end - span_s)
        if segs[0]['start_s'] >= t0:
            return segs, 0.0
        i = len(segs) - 1
        while i > 0 and segs[i - 1]['end_s'] > t0:
            i -= 1
        return segs[i:], t0

    def marks(self):
        return list(self._marks)

    def position_s(self):
        return self.pos_s

    def current_seg_index(self):
        if self.seg is None:
            return -1
        for i, s in enumerate(self.archive.snapshot()):
            if s['file'] == self.seg['file']:
                return i
        return -1

    def to_live(self):
        self.msg = '演示模式无直播源'
        self.msg_t = time.monotonic()

    def to_replay(self):
        if self.seg is not None:
            self.seek(self.seg['start_s'])

    def seek(self, pos_s):
        segs = self.archive.snapshot()
        if not segs:
            return
        end = segs[-1]['end_s']
        pos_s = min(max(0.0, pos_s), max(0.0, end - 0.05))
        seg = segs[-1]
        for s in segs:
            if s['start_s'] <= pos_s < s['end_s']:
                seg = s
                break
        self.ended = False
        self.paused = False
        if self.cap is not None and self.seg is not None \
                and seg['file'] == self.seg['file']:
            self._jump(pos_s)
            return
        info = self.loader.clip(seg['file'])
        if info:
            self._open_seg(seg, info, pos_s)

    def preview_seek(self, pos_s):
        self.pos_s = pos_s

    def _jump(self, pos_s):
        if self.cap is None:
            return
        f = int(round((pos_s - self.seg['start_s']) * self.fps))
        f = max(0, min(self.frame_count - 1, f))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx = f + 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
            self.ended = False

    def _open_seg(self, seg, info, pos_s):
        if self.cap is not None:
            self.cap.release()
        cap = cv2.VideoCapture(info['mkv'])
        self.cap = cap
        self.seg = seg
        self.fps = cap.get(cv2.CAP_PROP_FPS) or info['fps']
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or info['frames']
        self.idx = 0
        self._jump(pos_s)

    def tick(self):
        if self.cap is None or self.paused or self.ended:
            return None
        t0 = time.perf_counter()
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx += 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
        else:
            nxt_i = self.current_seg_index() + 1
            segs = self.archive.snapshot()
            if nxt_i < len(segs):
                seg = segs[nxt_i]
                info = self.loader.clip(seg['file'])
                self._open_seg(seg, info, seg['start_s'])
            else:
                self.ended = True
                self.paused = True
            return None
        target = 1.0 / (self.fps * (1.0, 0.5, 0.25)[self.speed_idx])
        return target - (time.perf_counter() - t0)

    def _fit(self, img):
        w = self.args.display_width
        if not w or img.shape[1] == w:
            return img
        h = int(round(img.shape[0] * w / img.shape[1]))
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    def toggle_play(self):
        if self.cap is None:
            return
        if self.ended:
            self.paused = False
            self.ended = False
            self._jump(self.seg['start_s'])
        else:
            self.paused = not self.paused

    def step(self, delta):
        if self.cap is None:
            return
        self.paused = True
        self.ended = False
        f = max(0, min(self.frame_count - 1, self.idx - 1 + delta))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx = f + 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
        else:
            self.ended = True

    def set_speed(self, idx):
        self.speed_idx = idx
        self.msg = '速度 %.2fx' % (1.0, 0.5, 0.25)[idx]
        self.msg_t = time.monotonic()

    def jump_to_segment(self, i):
        segs = self.archive.snapshot()
        if 0 <= i < len(segs):
            self.seek(segs[i]['start_s'])

    def add_mark(self):
        t = self.position_s()
        self._mark_seq += 1
        self._marks.append({'t_s': t, 'note': '标记 %d' % self._mark_seq,
                            'wall': time.time()})

    def jump_to_mark(self, i):
        if 0 <= i < len(self._marks):
            self.seek(self._marks[i]['t_s'])

    def start_recording(self):
        self.msg = '演示模式无录制功能'
        self.msg_t = time.monotonic()

    def stop_recording(self):
        pass

    def snapshot(self):
        if self.drawn is None:
            return
        os.makedirs(self.args.out_dir, exist_ok=True)
        p = os.path.join(self.args.out_dir,
                         time.strftime('shot_%Y%m%d_%H%M%S.jpg'))
        cv2.imwrite(p, self.drawn)
        self.msg = '截图: %s' % p
        self.msg_t = time.monotonic()

    def reset_clock(self):
        self.clock_offset = 0.0
        self.clock_t0 = time.monotonic()


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--clip', action='append', default=None)
    ap.add_argument('--cache-dir', default='/home/ifit/xjh/replay/cache')
    ap.add_argument('--out-dir', default='/home/ifit/xjh/replay')
    ap.add_argument('--display-width', type=int, default=1280)
    ap.add_argument('--window-width', type=int, default=1600)
    ap.add_argument('--window-height', type=int, default=1000)
    ap.add_argument('--speed', type=int, default=0, choices=(0, 1, 2))
    return ap.parse_args()


def main():
    args = parse_args()
    if not os.environ.get('DISPLAY'):
        print('没有 DISPLAY')
        return 2
    # 选片段：指定优先，否则用缓存里较小的前 3 个
    paths = args.clip
    if not paths:
        cand = sorted(glob.glob(os.path.join(args.cache_dir, '*.mkv')),
                      key=lambda p: os.path.getsize(p))
        paths = cand[-3:] if len(cand) >= 3 else cand
    clips = [(os.path.basename(os.path.splitext(p)[0]) + '.h265', p)
             for p in paths]
    if not clips:
        print('没有可用的本地片段')
        return 1

    import tkinter as tk
    from studio_ui import StudioUI

    root = tk.Tk()
    ctl = DemoController(args, clips)
    ui = StudioUI(root, ctl, args)

    def loop():
        if ctl.quit_requested():
            ui.root.destroy()
            return
        delay = ctl.tick()
        try:
            ui.refresh()
        except Exception as e:
            print('loop error: %s' % e, flush=True)
        interval = 33
        if delay is not None:
            interval = max(1, min(33, int(delay * 1000)))
        root.after(interval, loop)

    root.after(30, loop)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
