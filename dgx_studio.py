#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DGX 监看台：直播常开，随时回放已存片段（全部鼠标操作）

用法:
  /usr/bin/python3 dgx_studio.py
  /usr/bin/python3 dgx_studio.py --speed 1 --display-width 1280

窗口:
  顶部按钮：START STOP LIVE REPLAY PLAY/PAUSE |< >| 1x 0.5x 0.25x SHOT QUIT
  START/STOP 通过 ssh 控制 Jetson 录制管线（直播源），STOP 需点两次确认
  顶部时间轴：拖动到任意已封存时刻
  回放时右下角小窗继续显示直播，点 LIVE 立即回直播
"""
import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

import cv2

from dgx_viewer import LiveDecoder, connect, overlay, placeholder, snapshot
from hk_replay_fetch import remux_mkv

WIN = 'DGX Studio'
TB_TIME = 'Timeline'
SPEEDS = (1.0, 0.5, 0.25)
BTNS = (
    ('start', 'START', 100),
    ('stop', 'STOP', 112),
    ('live', 'LIVE', 100),
    ('replay', 'REPLAY', 120),
    ('play', 'PAUSE', 120),
    ('back', '|<', 58),
    ('fwd', '>|', 58),
    ('s1', '1x', 58),
    ('s2', '0.5x', 70),
    ('s3', '0.25x', 84),
    ('shot', 'SHOT', 84),
    ('quit', 'QUIT', 84),
)
BTN_GAP = 8
BTN_TOP = 12
BTN_H = 54
TEXT_Y = 96


def fmt_t(s):
    s = max(0, int(s))
    if s >= 3600:
        return '%d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60)
    return '%02d:%02d' % (s // 60, s % 60)


class Archive(threading.Thread):
    """轮询 Jetson 当前录制目录的 meta.jsonl，拿到已封存分段"""

    def __init__(self, remote, root, interval=3.0):
        super().__init__(daemon=True)
        self.remote = remote
        self.root = root
        self.interval = interval
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.segments = []
        self.session = ''
        self.error = ''

    def snapshot(self):
        with self.lock:
            return list(self.segments)

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self._refresh()
            except Exception as e:
                with self.lock:
                    self.error = '%s: %s' % (type(e).__name__, e)
            self.stop_evt.wait(self.interval)

    def _refresh(self):
        cmd = ('d=$(ls -dt %s/stage1_* 2>/dev/null | head -1); '
               'if [ -n "$d" ]; then echo "DIR $d"; '
               'cat "$d/meta.jsonl" 2>/dev/null; fi') % self.root
        p = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
             self.remote, cmd],
            capture_output=True, text=True, timeout=20)
        session = ''
        segs = []
        for line in p.stdout.splitlines():
            if line.startswith('DIR '):
                session = line[4:].strip()
            elif line.startswith('{'):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rec['dir'] = session
                rec['start_s'] = rec['start_ms'] / 1000.0
                rec['end_s'] = rec['end_ms'] / 1000.0
                segs.append(rec)
        with self.lock:
            if session:
                self.session = session
                self.segments = segs
                self.error = '' if p.returncode == 0 else p.stderr.strip()
            elif not p.stdout.strip():
                self.error = (p.stderr.strip() or '未找到录制目录')


class ClipLoader(threading.Thread):
    """按需拉取远端分段并转封装；缓存后可在段内任意定位"""

    def __init__(self, remote, cache_dir, cache_max_gb=20.0):
        super().__init__(daemon=True)
        self.remote = remote
        self.cache_dir = cache_dir
        self.cache_max_gb = cache_max_gb
        os.makedirs(cache_dir, exist_ok=True)
        self.cv = threading.Condition()
        self.want = None
        self.clips = {}
        self.errors = {}
        self.loading = ''
        self.proc = None
        self.stop_evt = threading.Event()

    def request(self, seg, idle_only=False):
        with self.cv:
            if idle_only and (self.want is not None or self.loading):
                return
            self.want = dict(seg)
            self.cv.notify()

    def clip(self, name):
        with self.cv:
            return self.clips.get(name)

    def error(self, name):
        with self.cv:
            return self.errors.get(name)

    def stop(self):
        with self.cv:
            self.stop_evt.set()
            self.cv.notify()
            proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self):
        while not self.stop_evt.is_set():
            with self.cv:
                while self.want is None and not self.stop_evt.is_set():
                    self.cv.wait(0.5)
                if self.stop_evt.is_set():
                    return
                seg, self.want = self.want, None
                self.loading = seg['file']
            try:
                info = self._ensure(seg)
                if self.cache_max_gb > 0:
                    self._enforce_cache()
                with self.cv:
                    self.clips[seg['file']] = info
                    self.errors.pop(seg['file'], None)
            except Exception as e:
                with self.cv:
                    self.errors[seg['file']] = '%s: %s' % (
                        type(e).__name__, e)
            finally:
                with self.cv:
                    self.loading = ''

    def _ensure(self, seg):
        key = os.path.basename(seg['dir'].rstrip('/')) + '__' + seg['file']
        mkv = os.path.join(self.cache_dir, key[:-5] + '.mkv')
        dur = seg['end_s'] - seg['start_s']
        fps = seg['frames'] / dur if dur > 0 else 59.94
        if not os.path.exists(mkv):
            part = mkv[:-4] + '.part.h265'
            remote = '%s:%s/%s' % (self.remote, seg['dir'], seg['file'])
            try:
                with self.cv:
                    self.proc = subprocess.Popen(
                        ['scp', '-q', '-o', 'BatchMode=yes',
                         '-o', 'ConnectTimeout=6', remote, part],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
                    proc = self.proc
                _, err = proc.communicate(timeout=180)
                if proc.returncode != 0:
                    raise RuntimeError('scp failed %s' % err.strip())
                if not remux_mkv(part, mkv, seg['frames'], fps):
                    raise RuntimeError('remux frame count mismatch')
            finally:
                with self.cv:
                    self.proc = None
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
        return {'mkv': mkv, 'fps': fps,
                'start_s': seg['start_s'], 'frames': seg['frames']}

    def _enforce_cache(self):
        items = []
        for name in os.listdir(self.cache_dir):
            if not name.endswith('.mkv'):
                continue
            path = os.path.join(self.cache_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            items.append((st.st_mtime, st.st_size, path))
        total = sum(sz for _, sz, _ in items)
        limit = self.cache_max_gb * 1e9
        for _, sz, path in sorted(items):
            if total <= limit:
                break
            try:
                os.remove(path)
                total -= sz
            except OSError:
                pass


class Studio:
    def __init__(self, args):
        self.args = args
        self.stop_evt = threading.Event()
        self.archive = Archive(args.remote, args.remote_root,
                               args.archive_interval)
        self.loader = ClipLoader(args.remote, args.cache_dir,
                                 args.cache_max_gb)

        # 直播：后台常开，回放时也不断开
        self.q = queue.Queue(4)
        self.live_evt = threading.Event()
        self.live_dec = None
        self.live_sock = None
        self.live_img = None
        self.live_img_t = 0.0
        self.live_ts = deque(maxlen=128)
        self.live_wanted = True
        self.live_next_try = 0.0
        self.live_fast_until = 0.0

        # 回放状态
        self.mode = 'live'
        self.speed_idx = args.speed
        self.remote_busy = False
        self.stop_confirm_t = 0.0
        self.cap = None
        self.seg = None
        self.idx = 0
        self.fps = 59.94
        self.frame_count = 0
        self.pos_s = 0.0
        self.paused = False
        self.ended = False
        self.pending = None
        self.view = None
        self.drawn = None
        self.buttons = {}
        self.tb_max = 10
        self.tb_last = 0
        self.tb_guard = False
        self.tb_req_s = None
        self.tb_req_ts = 0.0
        self.tb_user_ts = 0.0
        self.tb_follow_ts = 0.0
        self.msg = ''
        self.msg_t = 0.0

    # ---------- 直播 ----------

    def _open_live(self):
        try:
            sock = connect(self.args.host, self.args.live_port)
        except OSError as e:
            delay = (1.5 if time.monotonic() < self.live_fast_until else 5.0)
            self.set_msg('live connect failed (%s); retry in %gs'
                         % (e, delay))
            self.live_next_try = time.monotonic() + delay
            return
        self.live_sock = sock
        self.q = queue.Queue(4)
        self.live_evt.clear()
        self.live_ts.clear()
        self.live_dec = LiveDecoder(sock, self.q, self.live_evt,
                                    self.args.display_width)
        self.live_dec.start()
        self.set_msg('live connected')

    def _close_live_sock(self):
        if self.live_sock is None:
            return
        try:
            self.live_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.live_sock.close()
        except OSError:
            pass
        self.live_sock = None

    def _check_live(self):
        if not self.live_wanted:
            return
        if self.live_dec is not None and self.live_dec.is_alive():
            return
        if self.live_dec is not None:
            self.live_dec = None
            self._close_live_sock()
            self.live_next_try = time.monotonic() + 5.0
            self.set_msg('live disconnected; retry in 5s')
        if time.monotonic() >= self.live_next_try:
            self._open_live()

    def _drain_live(self):
        while True:
            try:
                img = self.q.get_nowait()
            except queue.Empty:
                break
            if not self.live_wanted:
                continue
            self.live_img = img
            self.live_img_t = time.monotonic()
            self.live_ts.append(time.perf_counter())

    def _live_fps(self):
        now = time.perf_counter()
        while self.live_ts and now - self.live_ts[0] > 2.0:
            self.live_ts.popleft()
        if len(self.live_ts) < 2:
            return 0.0
        span = self.live_ts[-1] - self.live_ts[0]
        return (len(self.live_ts) - 1) / span if span > 0 else 0.0

    def _live_alive(self):
        return (self.live_img is not None
                and time.monotonic() - self.live_img_t < 3.0)

    # ---------- 远程录制控制 ----------

    def _ssh(self, cmd, timeout=20):
        return subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
             self.args.remote, cmd],
            capture_output=True, text=True, timeout=timeout)

    def _start_cmd(self):
        a = self.args
        # 只认 comm=python3 的进程，避免匹配到 ssh 远端 shell 自身
        check = ("ps -eo comm=,args= | awk '$1==\"python3\" && "
                 "/hk_pipeline_test[.]py/ {f=1} END {exit !f}'")
        return ('if %s; then echo ALREADY; exit 0; fi; '
                '( cd ~/hk_camera && exec setsid /usr/bin/python3 '
                'hk_pipeline_test.py --seconds 0 --segment %g --ring %g '
                '--live-port %d --min-free-gb %g --out %s ) '
                '< /dev/null > /tmp/hk_record.log 2>&1 & echo STARTED'
                % (check, a.rec_segment, a.rec_ring, a.live_port,
                   a.rec_min_free_gb, a.remote_root))

    def _stop_cmd(self):
        return ('for d in $(ls -dt %s/stage1_* 2>/dev/null); do '
                'if [ -f "$d/recorder.pid" ]; then '
                'p=$(cat "$d/recorder.pid"); '
                'if kill -TERM "$p" 2>/dev/null; then echo "STOPPING $p"; '
                'else echo "STALE $p"; fi; exit 0; fi; done; echo NO_PID'
                % self.args.remote_root)

    def _teardown_live(self):
        self.live_evt.set()
        self._close_live_sock()
        dec, self.live_dec = self.live_dec, None
        self.live_img = None
        self.live_img_t = 0.0
        self.live_ts.clear()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        if dec is not None:
            dec.join(timeout=1.5)

    def _remote_start_worker(self):
        self.remote_busy = True
        self.set_msg('starting Jetson recorder ...')
        try:
            p = self._ssh(self._start_cmd())
            out = (p.stdout + '\n' + p.stderr).strip()
            if 'ALREADY' in p.stdout:
                self.live_wanted = True
                self.live_next_try = 0.0
                self.set_msg('Jetson already recording')
            elif 'STARTED' in p.stdout:
                self.live_wanted = False
                self._teardown_live()
                self.live_wanted = True
                self.live_next_try = 0.0
                self.live_fast_until = time.monotonic() + 20.0
                self.set_msg('Jetson starting; waiting for live...')
            else:
                self.set_msg('START failed: %s'
                             % (out or 'rc=%d' % p.returncode))
        except Exception as e:
            self.set_msg('START failed: %s: %s' % (type(e).__name__, e))
        finally:
            self.remote_busy = False

    def _remote_stop_worker(self):
        self.remote_busy = True
        self.set_msg('stopping Jetson recorder ...')
        try:
            p = self._ssh(self._stop_cmd())
            out = (p.stdout + '\n' + p.stderr).strip()
            if 'STOPPING' in p.stdout or 'STALE' in p.stdout \
                    or 'NO_PID' in p.stdout:
                self.live_wanted = False
                self._teardown_live()
                if 'NO_PID' in p.stdout:
                    self.set_msg('Jetson recorder not running')
                else:
                    self.set_msg('Jetson recorder stopped')
            else:
                self.set_msg('STOP failed: %s'
                             % (out or 'rc=%d' % p.returncode))
        except Exception as e:
            self.set_msg('STOP failed: %s: %s' % (type(e).__name__, e))
        finally:
            self.remote_busy = False

    def _start_clicked(self):
        if self.remote_busy:
            return
        self.stop_confirm_t = 0.0
        threading.Thread(target=self._remote_start_worker,
                         daemon=True).start()

    def _stop_clicked(self):
        if self.remote_busy:
            return
        now = time.monotonic()
        if now > self.stop_confirm_t:
            self.stop_confirm_t = now + 5.0
            self.set_msg('Click STOP again within 5s to stop recording')
            return
        self.stop_confirm_t = 0.0
        threading.Thread(target=self._remote_stop_worker,
                         daemon=True).start()

    # ---------- 时间轴 ----------

    def _on_time_tb(self, val):
        if self.tb_guard or val == self.tb_last:
            return
        self.tb_last = val
        self.tb_req_s = val / 10.0
        self.tb_req_ts = time.monotonic()
        self.tb_user_ts = self.tb_req_ts

    def _poll_time_tb(self):
        if self.tb_req_s is None:
            return
        if time.monotonic() - self.tb_req_ts < 0.35:
            return
        pos = self.tb_req_s
        self.tb_req_s = None
        self.seek(pos)

    def _set_tb(self, val):
        val = max(0, int(round(val)))
        self.tb_guard = True
        self.tb_last = val
        try:
            cv2.setTrackbarPos(TB_TIME, WIN, val)
        except Exception:
            pass
        finally:
            self.tb_guard = False
        self.tb_follow_ts = time.monotonic()

    def _sync_tb(self):
        segs = self.archive.snapshot()
        end = segs[-1]['end_s'] if segs else 0.0
        want = max(10, int(end * 10))
        if want != self.tb_max:
            self.tb_max = want
            try:
                cv2.setTrackbarMax(TB_TIME, WIN, want)
            except Exception:
                pass
        now = time.monotonic()
        if now - self.tb_follow_ts < 0.5 or self.tb_req_s is not None:
            return
        if self.mode == 'live':
            self._set_tb(end * 10)
        elif (not self.pending and self.cap is not None
              and now - self.tb_user_ts > 2.0):
            self._set_tb(self.pos_s * 10)

    def _timeline_end(self):
        segs = self.archive.snapshot()
        return segs[-1]['end_s'] if segs else 0.0

    def _speed(self):
        return SPEEDS[self.speed_idx]

    def _fit(self, img):
        w = self.args.display_width
        if not w or img.shape[1] == w:
            return img
        h = int(round(img.shape[0] * w / img.shape[1]))
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    # ---------- 回放 ----------

    def to_replay(self):
        print('mode -> replay', flush=True)
        segs = self.archive.snapshot()
        if not segs:
            self.set_msg('no stored clips yet')
            return
        self.mode = 'replay'
        self.seek(max(0.0, segs[-1]['end_s'] - 1.0))

    def to_live(self):
        print('mode -> live', flush=True)
        self.mode = 'live'
        self.tb_req_s = None
        self._set_tb(self._timeline_end() * 10)

    def seek(self, pos_s):
        print('seek -> %.1fs' % pos_s, flush=True)
        segs = self.archive.snapshot()
        if not segs:
            self.set_msg('no stored clips yet')
            return
        end = segs[-1]['end_s']
        pos_s = min(max(0.0, pos_s), max(0.0, end - 0.05))
        seg = None
        for s in segs:
            if s['start_s'] <= pos_s < s['end_s']:
                seg = s
                break
        if seg is None:
            seg = segs[-1]
            pos_s = max(0.0, end - 0.1)
        self.mode = 'replay'
        self.ended = False
        self.paused = False
        if (self.cap is not None and self.seg is not None
                and seg['file'] == self.seg['file']):
            self._jump(pos_s)
            return
        info = self.loader.clip(seg['file'])
        if info is None:
            err = self.loader.error(seg['file'])
            if err:
                self.set_msg('replay load failed: %s' % err)
                return
            self.pending = (seg, pos_s)
            self.set_msg('preparing %s ...' % seg['file'])
            self.loader.request(seg)
            return
        self._open_seg(seg, info, pos_s)

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
            self.cap = None
        cap = cv2.VideoCapture(info['mkv'])
        if not cap.isOpened():
            self.set_msg('open replay failed: %s' % info['mkv'])
            return
        self.cap = cap
        self.seg = seg
        self.fps = cap.get(cv2.CAP_PROP_FPS) or info['fps']
        self.frame_count = (int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                            or info['frames'])
        self.idx = 0
        self.pending = None
        self._jump(pos_s)
        self.set_msg('ready %s  %d frames' % (seg['file'], self.frame_count))
        nxt = self._next_seg(seg)
        if nxt is not None:
            self.loader.request(nxt, idle_only=True)

    def _next_seg(self, seg):
        for s in self.archive.snapshot():
            if s['file'] != seg['file'] and s['start_s'] >= seg['end_s'] - 0.01:
                return s
        return None

    def _tick_replay(self):
        if self.pending is not None:
            seg, pos_s = self.pending
            info = self.loader.clip(seg['file'])
            if info is not None:
                self.pending = None
                self._open_seg(seg, info, pos_s)
            else:
                err = self.loader.error(seg['file'])
                if err:
                    self.pending = None
                    self.set_msg('replay load failed: %s' % err)
            return None
        if self.cap is None or self.paused or self.ended:
            return None
        t0 = time.perf_counter()
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx += 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
        else:
            nxt = self._next_seg(self.seg)
            if nxt is None:
                self.ended = True
                self.paused = True
            else:
                info = self.loader.clip(nxt['file'])
                if info is not None:
                    self._open_seg(nxt, info, nxt['start_s'])
                else:
                    self.pending = (nxt, nxt['start_s'])
                    self.loader.request(nxt)
            return None
        target = 1.0 / (self.fps * self._speed())
        return target - (time.perf_counter() - t0)

    def _step(self, delta):
        if self.mode != 'replay' or self.cap is None:
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

    # ---------- 绘制 / 交互 ----------

    def set_msg(self, text):
        self.msg = text
        self.msg_t = time.monotonic()
        print(text, flush=True)

    def _compose(self):
        if self.mode == 'live':
            if self.live_img is not None:
                view = self.live_img.copy()
            elif self.live_wanted:
                view = placeholder('LIVE: waiting for Jetson...',
                                   self.args.display_width)
            else:
                view = placeholder('LIVE OFF: click START',
                                   self.args.display_width)
            if not self.live_wanted:
                status = ('LIVE OFF  click START (top-left) '
                          'to launch Jetson recorder')
            else:
                status = 'LIVE  %.1f fps   recorded %s' % (
                    self._live_fps() if self._live_alive() else 0.0,
                    fmt_t(self._timeline_end()))
            lines = [
                status,
                'Click REPLAY to review, drag Timeline to seek',
                '',
            ]
        else:
            if self.drawn is not None:
                view = self.drawn.copy()
            else:
                view = placeholder('REPLAY: preparing...',
                                   self.args.display_width)
            state = '  END' if self.ended else (
                '  PAUSED' if self.paused else '')
            segs = self.archive.snapshot()
            n_seg = 0
            if self.seg is not None:
                for i, s in enumerate(segs):
                    if s['file'] == self.seg['file']:
                        n_seg = i
                        break
            seg_label = '-' if self.seg is None else str(n_seg + 1)
            lines = [
                'REPLAY  %s / %s  %.2fx%s  seg %s/%d' % (
                    fmt_t(self.pos_s), fmt_t(self._timeline_end()),
                    self._speed(), state, seg_label, max(1, len(segs))),
                'PLAY/PAUSE  |< >| frames  1x 0.5x 0.25x  LIVE',
                'live background %.1f fps' % (
                    self._live_fps() if self._live_alive() else 0.0)
                if self.live_wanted else 'live off',
            ]
            if self.live_img is not None and self._live_alive():
                view = self._pip(view)
        if self.msg and time.monotonic() - self.msg_t < 6.0:
            lines.append(self.msg)
        overlay(view, [x for x in lines if x], TEXT_Y)
        self._draw_buttons(view)
        self.drawn_size = (view.shape[1], view.shape[0])
        return view

    def _pip(self, view):
        h, w = view.shape[:2]
        pw = max(160, int(w * 0.28))
        ph = int(pw * self.live_img.shape[0] / self.live_img.shape[1])
        x0 = w - pw - 16
        y0 = h - ph - 18
        small = cv2.resize(self.live_img, (pw, ph),
                           interpolation=cv2.INTER_AREA)
        view[y0:y0 + ph, x0:x0 + pw] = small
        cv2.rectangle(view, (x0 - 2, y0 - 2), (x0 + pw + 1, y0 + ph + 1),
                      (0, 255, 0), 2)
        cv2.putText(view, 'LIVE', (x0 + 8, y0 + 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(view, 'LIVE', (x0 + 8, y0 + 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 1, cv2.LINE_AA)
        return view

    def _btn_enabled(self, name):
        if name in ('start', 'stop'):
            return not self.remote_busy
        if name in ('live', 'replay', 'shot', 'quit'):
            return True
        return self.mode == 'replay'

    def _draw_buttons(self, view):
        h, w = view.shape[:2]
        total = sum(b[2] for b in BTNS) + BTN_GAP * (len(BTNS) - 1)
        s = min(1.0, (w - 24.0) / total)
        x = (w - total * s) / 2.0
        self.buttons = {}
        for key, label, bw in BTNS:
            r = (int(x), int(BTN_TOP * s),
                 int(x + bw * s), int((BTN_TOP + BTN_H) * s))
            self.buttons[key] = r
            enabled = self._btn_enabled(key)
            active = ((key == 'live' and self.mode == 'live')
                      or (key == 'replay' and self.mode == 'replay')
                      or (self.mode == 'replay' and self.speed_idx ==
                          {'s1': 0, 's2': 1, 's3': 2}.get(key)))
            confirming = (key == 'stop'
                          and time.monotonic() < self.stop_confirm_t)
            if key == 'quit':
                base = (125, 45, 45)
            elif key == 'start':
                base = (0, 110, 0) if enabled else (34, 34, 34)
            elif key == 'stop':
                if not enabled:
                    base = (34, 34, 34)
                elif confirming:
                    base = (0, 0, 225)
                else:
                    base = (110, 40, 40)
            elif active:
                base = (0, 135, 0)
            elif enabled:
                base = (62, 62, 62)
            else:
                base = (34, 34, 34)
            cv2.rectangle(view, r[:2], r[2:], base, -1)
            cv2.rectangle(view, r[:2], r[2:],
                          (220, 220, 220) if enabled else (75, 75, 75), 1)
            text = label
            if confirming:
                text = 'CONFIRM'
            elif key == 'play':
                text = ('PAUSE' if (self.mode == 'replay'
                                    and not self.paused and not self.ended)
                        else 'PLAY')
            fs = max(0.4, 0.62 * s)
            th = 2 if s > 0.55 else 1
            ts = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, th)[0]
            cx = r[0] + (r[2] - r[0] - ts[0]) // 2
            cy = r[1] + (r[3] - r[1] + ts[1]) // 2
            color = (255, 255, 255) if enabled else (115, 115, 115)
            cv2.putText(view, text, (cx, cy), cv2.FONT_HERSHEY_SIMPLEX,
                        fs, color, th, cv2.LINE_AA)
            x += (bw + BTN_GAP) * s

    def _click(self, name):
        print('click %s' % name, flush=True)
        if name == 'start':
            self._start_clicked()
        elif name == 'stop':
            self._stop_clicked()
        elif name == 'live':
            self.to_live()
        elif name == 'replay':
            self.to_replay()
        elif name == 'shot':
            if self.drawn is not None:
                snapshot(self.drawn, self.args.out_dir)
        elif name == 'quit':
            self.stop()
        elif not self._btn_enabled(name):
            return
        elif name == 'play':
            self._toggle_pause()
        elif name == 'back':
            self._step(-1)
        elif name == 'fwd':
            self._step(1)
        elif name in ('s1', 's2', 's3'):
            self.speed_idx = int(name[1]) - 1
            self.set_msg('speed %.2fx' % self._speed())

    def _toggle_pause(self):
        if self.cap is None:
            return
        if self.ended:
            self.paused = False
            self.ended = False
            self._jump(self.seg['start_s'])
        else:
            self.paused = not self.paused

    def _on_mouse(self, ev, x, y, _flags, _param):
        if ev != cv2.EVENT_LBUTTONDOWN or not self.buttons:
            return
        try:
            _ix, _iy, iw, ih = cv2.getWindowImageRect(WIN)
        except Exception:
            return
        if iw <= 0 or ih <= 0:
            return
        print('mouse down win=(%d,%d) img_rect=(%d,%d,%d,%d)'
              % (x, y, _ix, _iy, iw, ih), flush=True)
        # GTK 后端的鼠标坐标以图像控件左上角为原点，
        # 而 getWindowImageRect 返回的是屏幕坐标，不能用来做偏移
        vw, vh = self.drawn_size
        vx = x * vw / iw
        vy = y * vh / ih
        for name, r in self.buttons.items():
            if r[0] <= vx <= r[2] and r[1] <= vy <= r[3]:
                self._click(name)
                return

    def _handle_key(self, key, out_dir):
        if key in (ord('q'), 27):
            self.stop()
        elif key == ord('s') and self.drawn is not None:
            snapshot(self.drawn, out_dir)
        elif key == ord('l'):
            self.to_live()
        elif key == ord('r'):
            self.to_replay()
        elif key == ord(' ') and self.mode == 'replay':
            self._toggle_pause()
        elif key in (81, ord('a')):
            self._step(-1)
        elif key in (83, ord('d')):
            self._step(1)
        elif key in (ord('1'), ord('2'), ord('3')):
            self.speed_idx = key - ord('1')

    # ---------- 主循环 ----------

    def _schedule(self):
        a = self.args
        ev = []
        if a.auto_seek is not None:
            ev.append((a.auto_seek_at, 'seek', a.auto_seek))
        if a.auto_speed is not None:
            ev.append((a.auto_speed_at, 'speed', a.auto_speed))
        if a.auto_live_at is not None:
            ev.append((a.auto_live_at, 'live', None))
        if a.auto_replay_at is not None:
            ev.append((a.auto_replay_at, 'replay', None))
        for item in a.auto_click or ():
            try:
                ts, name = item.split(':', 1)
                ev.append((float(ts), 'click', name))
            except ValueError:
                pass
        if a.auto_quit_at is not None:
            ev.append((a.auto_quit_at, 'quit', None))
        return sorted(ev)

    def _run_auto(self, events, t):
        while events and t >= events[0][0]:
            _, action, arg = events.pop(0)
            if action == 'seek':
                self.seek(arg)
            elif action == 'speed':
                self.speed_idx = int(arg)
            elif action == 'live':
                self.to_live()
            elif action == 'replay':
                self.to_replay()
            elif action == 'click':
                self._click(arg)
            elif action == 'quit':
                self.stop()

    def run(self):
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL | cv2.WINDOW_GUI_NORMAL)
        try:
            cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
        except Exception:
            pass
        cv2.resizeWindow(WIN, self.args.window_width,
                         self.args.window_height)
        cv2.createTrackbar(TB_TIME, WIN, 0, self.tb_max,
                           self._on_time_tb)
        cv2.setMouseCallback(WIN, self._on_mouse)

        self.archive.start()
        self.loader.start()
        self._open_live()
        events = self._schedule()
        t0 = time.monotonic()
        print('监看台已启动：直播常开，回放读 %s:%s' % (
            self.args.remote, self.args.remote_root), flush=True)

        while not self.stop_evt.is_set():
            t = time.monotonic() - t0
            self._run_auto(events, t)
            self._check_live()
            self._drain_live()
            self._poll_time_tb()
            delay = self._tick_replay() if self.mode == 'replay' else None
            self.view = self._compose()
            cv2.imshow(WIN, self.view)
            wait = 5 if delay is None else max(1, int(delay * 1000))
            key = cv2.waitKey(wait) & 0xFF
            self._handle_key(key, self.args.out_dir)
            self._sync_tb()
            try:
                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break

    def stop(self):
        self.stop_evt.set()

    def close(self):
        self.stop_evt.set()
        self.live_evt.set()
        self._close_live_sock()
        if self.live_dec is not None:
            self.live_dec.join(timeout=3.0)
            self.live_dec = None
        self.archive.stop_evt.set()
        self.archive.join(timeout=3.0)
        self.loader.stop()
        self.loader.join(timeout=5.0)
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        cv2.destroyAllWindows()


def main():
    ap = argparse.ArgumentParser()
    # 直连时是 IPv6 link-local（fe80::...%enP7s7），接交换机后改用固定 IPv4。
    # 现场 IP 若不同，用 --host 覆盖即可。
    ap.add_argument('--host', default='192.168.123.2')
    ap.add_argument('--live-port', type=int, default=9101)
    ap.add_argument('--remote', default='jetson', help='ssh 别名')
    ap.add_argument('--remote-root', default='~/hk_record')
    ap.add_argument('--cache-dir', default='/home/ifit/xjh/replay/cache')
    ap.add_argument('--cache-max-gb', type=float, default=20.0)
    ap.add_argument('--out-dir', default='/home/ifit/xjh/replay')
    ap.add_argument('--display-width', type=int, default=1280)
    ap.add_argument('--window-width', type=int, default=1300)
    ap.add_argument('--window-height', type=int, default=1160)
    ap.add_argument('--speed', type=int, default=0, choices=(0, 1, 2),
                    help='默认倍速挡位 0=1x 1=0.5x 2=0.25x')
    ap.add_argument('--archive-interval', type=float, default=3.0)
    ap.add_argument('--rec-segment', type=float, default=10.0,
                    help='START 时远端分段秒数')
    ap.add_argument('--rec-ring', type=float, default=90.0,
                    help='START 时远端环形缓冲秒数')
    ap.add_argument('--rec-min-free-gb', type=float, default=10.0,
                    help='START 时远端磁盘守护阈值')
    ap.add_argument('--auto-seek', type=float, default=None,
                    help='自检用：启动后跳到的秒数')
    ap.add_argument('--auto-seek-at', type=float, default=6.0)
    ap.add_argument('--auto-speed', type=int, default=None, choices=(0, 1, 2))
    ap.add_argument('--auto-speed-at', type=float, default=8.0)
    ap.add_argument('--auto-live-at', type=float, default=None)
    ap.add_argument('--auto-replay-at', type=float, default=None)
    ap.add_argument('--auto-click', action='append', default=None,
                    help='自检用：秒数:按钮名，可重复（start/stop/replay/live/s1/s2/s3/play/back/fwd/shot/quit）')
    ap.add_argument('--auto-quit-at', type=float, default=None)
    args = ap.parse_args()

    if not os.environ.get('DISPLAY'):
        print('没有 DISPLAY，无法开窗口')
        return 2

    st = Studio(args)

    def _sig(_s, _f):
        st.stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    try:
        st.run()
    finally:
        st.close()
    print('监看台已退出', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
