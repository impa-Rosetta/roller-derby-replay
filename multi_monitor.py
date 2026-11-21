#!/usr/bin/env python3
"""Four independent RK3588 controllers, one Tk window and one decoder per node."""
import argparse
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import socket
import struct
import sys
import threading
import time
import uuid

import cv2
import tkinter as tk

from hk_replay_fetch import HDR, recv_all, remux_mkv
from launch_monitor import load_nodes
from studio_app_rk import StudioController, parse_args
from studio_ui_rk import StudioUI
import studio_theme as T

LOG = logging.getLogger('multi_monitor')


class UnifiedController(StudioController):
    """Keep independent playback clocks when several controllers share a loop."""
    ring_generation = 0

    def to_live(self):
        self.ring_generation += 1
        return super().to_live()

    def to_replay(self):
        self.ring_generation += 1
        if self.opened is not None:
            ring_root = Path(self.args.out_dir).parent / 'ring-cache'
            if Path(self.opened['dir']).parent == ring_root:
                self.close_session(quiet=True)
        return super().to_replay()

    def tick(self):
        if (self.mode == 'replay' and self.cap is not None
                and self.pending is None and not self.paused and not self.ended
                and self._next_tick > time.perf_counter()):
            return self._next_tick - time.perf_counter()
        return super().tick()

    def _open_capture(self, info):
        from replay_capture_rk import open_replay
        return open_replay(info, self.args.display_width, self._say)


class EmbeddedStudioUI(StudioUI):
    """Open automatic modal prompts outside the shared playback timer."""
    _archive_scheduled = False

    def _update_history_ui(self, state):
        super()._update_history_ui(state)
        opened = self.ctl.opened
        ring_root = Path(self.args.out_dir).parent / 'ring-cache'
        if opened is not None and Path(opened['dir']).parent == ring_root:
            self.badge_hist.set('即时回放', T.WARN_AMBER)
            self.lbl_src.config(text='最近片段 · 直播持续接收')
            self.lbl_session.config(text='最近 %.0f 秒' % self.ctl.timeline_end())

    def _maybe_archive_dialog(self):
        if self._archive_scheduled or self._dialog_open:
            return
        self._archive_scheduled = True

        def prompt():
            try:
                if self.root.winfo_exists():
                    super(EmbeddedStudioUI, self)._maybe_archive_dialog()
            finally:
                self._archive_scheduled = False
        self.root.winfo_toplevel().after(1, prompt)


def node_args(node, state_root, defaults):
    from copy import copy
    args = copy(defaults)
    state = state_root / node['id']
    settings = state / 'state'
    settings.mkdir(parents=True, exist_ok=True)
    archive = settings / 'archive.json'
    if not archive.exists():
        archive.write_text(json.dumps({'dir': str(Path.home() / '录像_rk' / node['id'])},
                                      ensure_ascii=False), encoding='utf-8')
    args.host = node['host']
    args.remote = node['remote']
    args.remote_root = node.get('remote_root', '~/hk_record')
    args.live_port = int(node.get('live_port', 9101))
    args.thumb_port = 0
    args.referee_port = int(node['referee_port'])
    args.cache_dir = str(state / 'cache')
    args.out_dir = str(settings)
    return args


def fetch_recent(node, seconds, destination, base_ms, progress=lambda n: None):
    """Snapshot port 9100 without waiting for the disk segment to be sealed.

    Output uses the existing session/ClipLoader schema. Temporary directories
    live only below the per-node ring cache; recordings are never removed.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    name = 'recent_' + uuid.uuid4().hex + '.h265'
    raw_path = destination / name
    mkv = destination / (name[:-5] + '.mkv')
    try:
        with socket.create_connection((node['host'], int(node.get('replay_port', 9100))),
                                      timeout=8) as sock:
            sock.settimeout(15)
            sock.sendall(struct.pack('<I', seconds))
            header = recv_all(sock, HDR.size)
            if header is None:
                raise RuntimeError('回放服务未返回完整头部')
            magic, requested, start, end, frames, size = HDR.unpack(header)
            if (magic != b'HKR1' or requested != seconds or end <= start
                    or not 0 < frames < 60000 or not 0 < size <= 512 * 1024**2):
                raise RuntimeError('环形缓存暂无可用回放或响应无效')
            got = 0
            deadline = time.monotonic() + 90
            with raw_path.open('wb') as out:
                while got < size:
                    if time.monotonic() > deadline:
                        raise TimeoutError('拉取回放超过 90 秒')
                    block = sock.recv(min(1024**2, size - got))
                    if not block:
                        raise RuntimeError('回放传输中断 (%d/%d)' % (got, size))
                    out.write(block)
                    got += len(block)
                    progress(int(got * 100 / size))
        duration = (end - start) / 1000
        fps = frames / duration
        if not remux_mkv(str(raw_path), str(mkv), frames, fps):
            raise RuntimeError('回放转封装帧数校验失败')
        record = dict(file=name, start_ms=0, end_ms=end-start,
                      frames=frames, bytes=size)
        if base_ms is not None:
            record['base_ms'] = base_ms + start
        (destination / 'meta.jsonl').write_text(json.dumps(record) + '\n')
        info = dict(mkv=str(mkv), fps=fps, start_s=0, frames=frames)
        LOG.info('%s recent %ss: %.2fs %d frames %.2ffps %.1fMB',
                 node['id'], seconds, duration, frames, fps, size/1e6)
        return name, info
    except Exception:
        shutil.rmtree(destination)
        raise


class CameraTile:
    def __init__(self, parent, app, index):
        self.app, self.index = app, index
        self.photo = None
        self.last_frame = None
        self.last_key = None
        self.frame = tk.Frame(parent, bg=T.BG_PANEL, highlightbackground=T.BORDER,
                              highlightthickness=1)
        header = tk.Frame(self.frame, bg=T.BG_PANEL)
        header.pack(fill='x', padx=12, pady=6)
        node = app.nodes[index]
        self.title = tk.Label(header, text='%s  ·  %s' % (node['id'].upper(), node['host']),
                             bg=T.BG_PANEL, fg=T.FG_TEXT, font=T.F_TITLE, cursor='hand2')
        self.title.pack(side='left')
        self.status = tk.Label(header, bg=T.BG_PANEL, fg=T.FG_MUTED, font=T.F_SMALL)
        self.status.pack(side='right')
        self.canvas = tk.Canvas(self.frame, bg='#06090d', highlightthickness=0,
                                cursor='hand2', width=400, height=220)
        self.canvas.pack(fill='both', expand=True, padx=1)
        self.canvas.bind('<Button-1>', lambda e: app.show_detail(index))
        self.title.bind('<Button-1>', lambda e: app.show_detail(index))
        controls = tk.Frame(self.frame, bg=T.BG_PANEL)
        controls.pack(fill='x', padx=8, pady=5)
        for text, fn in [('放大', lambda: app.show_detail(index)),
                         ('直播', lambda: app.controllers[index].to_live()),
                         ('最近 10 秒', lambda: app.recent(index, 10)),
                         ('最近 30 秒', lambda: app.recent(index, 30))]:
            app.button(controls, text, fn).pack(side='left', padx=3)
        self.clock = tk.Label(controls, bg=T.BG_PANEL, fg=T.FG_MUTED, font=T.F_SMALL)
        self.clock.pack(side='right')
        self.note = tk.Label(self.frame, bg=T.BG_PANEL, fg=T.FG_MUTED,
                             anchor='w', font=T.F_SMALL)
        self.note.pack(fill='x', padx=12, pady=(0, 5))

    def refresh(self):
        ctl = self.app.controllers[self.index]
        st = ctl.state()
        mode = '直播' if ctl.mode == 'live' else ('暂停回放' if ctl.paused else '回放')
        self.status.config(text='%s  %.1f fps  ·  %s  ·  %s' % (
            '在线' if st['live_ok'] else '离线', st['live_fps'],
            '录制中' if st['recording'] else '未录制', mode),
            fg=T.LIVE_GREEN if st['live_ok'] else T.WARN_AMBER)
        self.clock.config(text=ctl.timecode())
        self.note.config(text=self.app.recent_status.get(self.index) or st['status'])
        img = ctl.current_frame()
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w <= 2 or h <= 2:
            return
        key = (id(img), w, h, ctl.mode, st['live_ok'])
        if key == self.last_key:
            return
        self.last_key = key
        self.last_frame = img  # retain reference so id cannot be recycled
        self.canvas.delete('all')
        if img is None:
            self.canvas.create_text(w//2, h//2, text=ctl.placeholder_text(),
                                    fill=T.FG_MUTED, font=T.F_TITLE)
            return
        ih, iw = img.shape[:2]
        scale = min(w/iw, h/ih)
        nw, nh = max(1, int(iw*scale)), max(1, int(ih*scale))
        rgb = cv2.cvtColor(cv2.resize(img, (nw, nh)), cv2.COLOR_BGR2RGB)
        if self.photo is None:
            self.photo = tk.PhotoImage(master=self.canvas)
        self.photo.configure(width=nw, height=nh)
        self.photo.put(b'P6 %d %d 255\n' % (nw, nh) + rgb.tobytes(), to=(0, 0))
        self.canvas.create_image(w//2, h//2, image=self.photo)
        if not st['live_ok'] and ctl.mode == 'live':
            self.canvas.create_text(w//2, 20, text='连接中断 · 等待重连（最后画面）',
                                    fill=T.WARN_AMBER, font=T.F_TITLE)


class MultiMonitor:
    def __init__(self, root, nodes, controllers, state_root, pad_enabled=True):
        self.root, self.nodes, self.controllers = root, nodes, controllers
        self.state_root = Path(state_root)
        self.selected = None
        self.detail = None
        self.detail_frame = None
        self.results = queue.Queue()
        self.recent_busy = set()
        self.recent_status = {}
        self.pads = []
        self.closing = False
        self.last_render = 0
        self.last_health = 0
        root.title('RK3588 四路监看台')
        root.configure(bg=T.BG_APP)
        root.geometry('1680x1000')
        root.minsize(1200, 800)
        root.protocol('WM_DELETE_WINDOW', self.close)
        bar = tk.Frame(root, bg=T.BG_PANEL)
        bar.pack(fill='x', padx=8, pady=8)
        self.button(bar, '四宫格 · Esc', self.show_grid).pack(side='left', padx=5)
        for i, node in enumerate(nodes):
            self.button(bar, node['id'].upper(), lambda i=i: self.show_detail(i)).pack(side='left', padx=3)
        self.heading = tk.Label(bar, text='四路独立直播 / 回放', fg=T.FG_TEXT,
                                bg=T.BG_PANEL, font=T.F_TITLE)
        self.heading.pack(side='left', padx=18)
        self.button(bar, '全部开始录制', self.start_all).pack(side='right', padx=5)
        self.button(bar, '最近 30 秒', lambda: self.recent_selected(30)).pack(side='right', padx=3)
        self.button(bar, '最近 10 秒', lambda: self.recent_selected(10)).pack(side='right', padx=3)
        self.content = tk.Frame(root, bg=T.BG_APP)
        self.content.pack(fill='both', expand=True)
        self.grid = tk.Frame(self.content, bg=T.BG_APP)
        self.grid.pack(fill='both', expand=True)
        self.tiles = []
        for i in range(len(nodes)):
            tile = CameraTile(self.grid, self, i)
            tile.frame.grid(row=i//2, column=i%2, sticky='nsew', padx=5, pady=5)
            self.tiles.append(tile)
        for i in (0, 1):
            self.grid.columnconfigure(i, weight=1, uniform='camera')
            self.grid.rowconfigure(i, weight=1, uniform='camera')
        self.footer = tk.Label(root, text='点击画面放大；每台相机独立回放。关闭窗口后设备继续录制。',
                               bg=T.BG_APP, fg=T.FG_MUTED, anchor='w', font=T.F_SMALL)
        self.footer.pack(fill='x', padx=12, pady=4)
        self._bind_keys()
        for ctl in controllers:
            ctl.start()
            if pad_enabled and ctl.args.referee_port:
                from referee_data_rk import PadServer
                pad = PadServer(ctl, port=ctl.args.referee_port)
                pad.start()
                self.pads.append(pad)
        root.after(30, self.loop)

    @staticmethod
    def button(parent, text, fn):
        return tk.Button(parent, text=text, command=fn, bg=T.BG_RAISED,
                         fg=T.FG_TEXT, activebackground=T.BG_HOVER,
                         activeforeground=T.FG_TEXT, relief='flat',
                         font=T.F_SMALL, padx=10, pady=5, cursor='hand2')

    def _bind_keys(self):
        self.root.bind('<Escape>', lambda e: self.show_grid() if self.root.grab_current() is None else None)
        keys = {'<space>': lambda c: c.toggle_play(), '<Left>': lambda c: c.step(-1),
                '<Right>': lambda c: c.step(1), '<F1>': lambda c: c.to_live(),
                '<F2>': lambda c: c.to_replay(), '<Home>': lambda c: c.seek(0),
                '1': lambda c: c.set_speed(0), '2': lambda c: c.set_speed(1),
                '3': lambda c: c.set_speed(2), '<F5>': lambda c: c.snapshot()}
        for key, fn in keys.items():
            self.root.bind(key, lambda e, fn=fn: self._key(fn))
        detail_keys = {
            'n': lambda ui: ui._whistle_note(),
            'm': lambda ui: ui._whistle(3.0), 'b': lambda ui: ui._whistle(0.0),
            '<Shift-Left>': lambda ui: ui.ctl.step(-10),
            '<Shift-Right>': lambda ui: ui.ctl.step(10),
            '<Control-Left>': lambda ui: ui._nudge(-5),
            '<Control-Right>': lambda ui: ui._nudge(5),
            '<Control-e>': lambda ui: ui.ctl.export_marks(),
            '<Control-s>': lambda ui: ui._on_whistle_lat(),
            '<Control-d>': lambda ui: ui._mark_fix(1),
            '<Control-f>': lambda ui: ui._mark_fix(-1),
            '<Control-r>': lambda ui: ui.ctl.reset_clock(),
            '<F9>': lambda ui: ui._begin_recording(),
            '<F10>': lambda ui: ui.ctl.stop_recording(),
        }
        for key, fn in detail_keys.items():
            self.root.bind(key, lambda e, fn=fn: self._key(lambda c: fn(self.detail)))

    def _key(self, fn):
        if self.selected is not None and self.root.grab_current() is None:
            focus = self.root.focus_get()
            if not isinstance(focus, (tk.Entry, tk.Text, tk.Listbox)):
                fn(self.controllers[self.selected])
                return 'break'

    def show_detail(self, index):
        if self.root.grab_current() is not None:
            return
        if self.selected == index:
            return
        self.grid.pack_forget()
        if self.detail_frame is not None:
            self.detail_frame.destroy()
        self.selected = index
        self.detail_frame = tk.Frame(self.content, bg=T.BG_APP)
        self.detail_frame.pack(fill='both', expand=True)
        ctl = self.controllers[index]
        self.detail = EmbeddedStudioUI(self.detail_frame, ctl, ctl.args, embedded=True)
        self.heading.config(text='%s · %s' % (self.nodes[index]['id'].upper(), ctl.args.host))
        self.root.focus_set()
        LOG.info('Detail %s', self.nodes[index]['id'])

    def show_grid(self):
        if self.root.grab_current() is not None:
            return
        self.selected = None
        if self.detail_frame is not None:
            self.detail_frame.destroy()
            self.detail_frame = self.detail = None
        self.grid.pack(fill='both', expand=True)
        self.heading.config(text='四路独立直播 / 回放')
        LOG.info('Grid')

    def start_all(self):
        for ctl in self.controllers:
            ctl.start_recording()

    def recent_selected(self, seconds):
        if self.selected is None:
            self.footer.config(text='请先点击某个相机，或使用各小画面下方的最近回放按钮。')
        else:
            self.recent(self.selected, seconds)

    def recent(self, index, seconds):
        if index in self.recent_busy:
            return
        ctl = self.controllers[index]
        ctl.ring_generation += 1
        generation = ctl.ring_generation
        self.recent_busy.add(index)
        self.recent_status[index] = '正在拉取最近 %d 秒…' % seconds
        ring_root = self.state_root / self.nodes[index]['id'] / 'ring-cache'
        ring_root.mkdir(parents=True, exist_ok=True)
        # Retain at most four successful snapshots plus the currently open one.
        protected = Path(ctl.opened['dir']) if ctl.opened else None
        old = sorted((p for p in ring_root.iterdir() if p.is_dir() and p.name.startswith('recent_')),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for path in old[3:]:
            if path != protected:
                shutil.rmtree(path)
        destination = ring_root / ('recent_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
        base = ctl.archive.base_ms()

        def work():
            try:
                result = fetch_recent(self.nodes[index], seconds, destination, base,
                                      lambda n: self.results.put(('progress', index, generation, n)))
                self.results.put(('ready', index, generation, (destination, result)))
            except Exception as exc:
                LOG.exception('Recent replay failed: %s', self.nodes[index]['id'])
                self.results.put(('error', index, generation, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def pump_results(self):
        while True:
            try:
                kind, index, generation, result = self.results.get_nowait()
            except queue.Empty:
                break
            ctl = self.controllers[index]
            if kind != 'progress':
                self.recent_busy.discard(index)
            if generation != ctl.ring_generation:
                self.recent_status.pop(index, None)
                continue
            if kind == 'progress':
                self.recent_status[index] = '拉取最近回放 %d%% · 直播继续' % result
            elif kind == 'error':
                self.recent_status[index] = '最近回放失败：' + result
            else:
                destination, (name, info) = result
                # Preloaded MKV prevents a duplicate transfer/remux and also
                # permits scrubbing the entire snapshot in the existing UI.
                with ctl.loader.cv:
                    ctl.loader.clips[name] = info
                ctl.open_session(str(destination))
                self.recent_status.pop(index, None)

    def loop(self):
        if self.closing:
            return
        if any(c.quit_requested() for c in self.controllers):
            self.close()
            return
        try:
            self.pump_results()
        except Exception:
            LOG.exception('Replay result failed')
        delay = .016
        for node, ctl in zip(self.nodes, self.controllers):
            try:
                next_frame = ctl.tick()
                if next_frame is not None:
                    delay = min(delay, max(.001, next_frame))
            except Exception:
                LOG.exception('Controller tick failed: %s', node['id'])
        for pad in self.pads:
            try:
                pad.pump()
            except Exception:
                LOG.exception('Pad request failed')
        now = time.monotonic()
        if now-self.last_render >= .05:
            self.last_render = now
            try:
                if self.selected is None:
                    for tile in self.tiles:
                        try:
                            tile.refresh()
                        except Exception:
                            LOG.exception('Tile render failed: %s', self.nodes[tile.index]['id'])
                else:
                    self.detail.refresh()
                    status = self.recent_status.get(self.selected)
                    if status:
                        self.footer.config(text=status)
                    else:
                        self.footer.config(text='Esc 返回四宫格 · 空格播放/暂停 · ← → 逐帧 · F1 直播 · F2 整场回放')
            except Exception:
                LOG.exception('Render failed')
        if now-self.last_health >= 10:
            self.last_health = now
            try:
                self.write_health()
            except OSError:
                LOG.exception('Cannot write monitor health')
        self.root.after(max(1, int(delay*1000)), self.loop)

    def write_health(self):
        state = {'view': 'grid' if self.selected is None else self.nodes[self.selected]['id'],
                 'updated': time.time(), 'nodes': {}}
        for node, ctl in zip(self.nodes, self.controllers):
            state['nodes'][node['id']] = dict(alive=ctl.feed.alive(), fps=ctl.live_fps(),
                image_n=ctl.feed.image_n, mode=ctl.mode, paused=ctl.paused,
                recording=ctl.recording, position=ctl.position_s(),
                timeline_end=ctl.timeline_end(), recent_busy=self.nodes.index(node) in self.recent_busy)
        target = self.state_root / 'multi-health.json'
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(state), encoding='utf-8')
        tmp.replace(target)

    def close(self):
        if self.closing:
            return
        self.closing = True
        for pad in self.pads:
            pad.stop()
        for ctl in self.controllers:
            ctl.shutdown()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='RK3588 四路统一监看台')
    parser.add_argument('--config', default=str(Path(__file__).with_name('nodes.local.json')))
    parser.add_argument('--no-pad', action='store_true', help='关闭平板 HTTP 服务')
    opts, extra = parser.parse_known_args()
    original = sys.argv
    try:
        sys.argv = [original[0]] + extra
        defaults = parse_args()
    finally:
        sys.argv = original
    nodes = load_nodes(opts.config)
    if len(nodes) != 4:
        parser.error('四路监看台要求配置恰好四台节点')
    state_root = Path.home() / '.local/share/roller-derby-replay'
    state_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(state_root/'multi-monitor.log'), level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    # Same locks as launch_monitor.py: no second decoder or PadServer can own
    # a node, even if its single-camera desktop shortcut is clicked.
    with contextlib.ExitStack() as stack:
        for node in nodes:
            path = state_root / node['id']
            path.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context((path/'monitor.lock').open('a'))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('请先关闭 %s 的单路/四路监看台。' % node['id'], flush=True)
                return 1
        controllers = []
        app = None
        try:
            for node in nodes:
                controllers.append(UnifiedController(node_args(node, state_root, defaults)))
            root = tk.Tk()
            app = MultiMonitor(root, nodes, controllers, state_root, pad_enabled=not opts.no_pad)
            LOG.info('Started four-camera monitor')
            root.mainloop()
        finally:
            if app is not None:
                for pad in app.pads:
                    pad.stop()
            for ctl in controllers:
                ctl.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
