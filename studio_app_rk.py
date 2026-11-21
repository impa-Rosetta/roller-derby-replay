#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛事监看台入口：控制器 + 主循环

用法:
  /usr/bin/python3 studio_app.py
  /usr/bin/python3 studio_app.py --window-width 2200 --window-height 1300

结构：
  StudioController  连接服务层（直播/归档/片段/远端控制）与 UI，
                    所有外部动作在这里统一调度，UI 只调 controller 方法。
  main()            装配 tkinter 主循环，~30fps 刷新。
"""
import argparse
import json
import os
import sys
import threading
import time

import cv2

import studio_services_rk as svc
from studio_services_rk import fmt_time, fmt_clock, fmt_hms


class StudioController:
    def __init__(self, args):
        self.args = args

        # RK3588 版接主码流（live_port）：设备侧砍掉了缩略流，没有 9102 可接。
        # 主码流 2448x2048@约59.2fps / 30Mbps 单路，DGX 解码实测够用。
        # （RK3588 版那边接的是缩略流，两边各走各的，互不影响。）
        feed_port = args.live_port
        self.feed = svc.LiveFeed(args.host, feed_port,
                                 args.display_width, want=True)
        self.archive = svc.Archive(args.remote, args.remote_root,
                                   args.archive_interval)
        # 本机归档目录先算出来：ClipLoader 要用它判断"这段已经在本地了"，
        # 归档后远端原文件已删，再走 scp 必然失败。
        self.archive_dir = self._load_archive_dir()
        self.loader = svc.ClipLoader(args.remote, args.cache_dir,
                                     args.cache_max_gb,
                                     local_dirs=[self.archive_dir])
        # 远端无场次时回退读本机归档的缓存（场次名 → 段列表）
        self._local_fb_name = None
        self._local_fb_segs = []
        self.remote = svc.RemoteCtrl(
            args.remote, args.remote_root, args.rec_segment, args.rec_ring,
            args.live_port, args.rec_min_free_gb, args.thumb_port)
        # 录像库：本机归档目录 + 用户手动加过的目录（上次记住的），
        # 只读它们的场次。归档目录永远排在第一位。
        self.library = svc.Library(
            [self.archive_dir], marks_path=os.path.join(args.out_dir,
                                                        'marks.jsonl'))
        self._load_library_roots()

        # ---- 状态 ----
        self.mode = 'live'          # 'live' | 'replay'
        self.speed_idx = int(args.speed)   # 下标：_speed() 拿它索引元组
        self.paused = False
        self.ended = False
        self.remote_busy = False
        self.recording = False      # RK3588 是否在录制（以 START/STOP 结果为准）
        self.stop_confirm_t = 0.0

        # ---- 归档 ----
        # 录制期间数据只落 RK3588 本地盘（录像绝不依赖网络），
        # archive_dir 只是停止后「拷回哪里去」的目标。
        self.archiver = svc.Archiver(args.remote, args.remote_root)
        # archive_dir 已在上面算好（loader.local_dirs 依赖它），别在这里重读。
        self.archive_busy = False
        self.archive_session = None      # 正在/待归档的场次名
        self.archive_done = 0
        self.archive_total = 0
        self.archive_phase = ''
        self.archive_cancel = None       # threading.Event，取消归档用
        self._pending_archive = None     # 停止后待确认归档的 (场次, info)
        self._pending_archive_sess = None   # 刚停止、待查概况的场次名
        self._archive_probe = None       # 后台查概况的线程
        self._archive_prompt = False     # UI 该弹确认框了
        self.clock_t0 = time.monotonic()   # 比赛计时起点
        self.clock_offset = 0.0
        self.msg = ''
        self.msg_t = 0.0

        # 回放
        self.cap = None
        self.seg = None
        self.fps = 59.2          # 录制端实际帧率（"59.2 FPS"），非 59.94
        self.frame_count = 0
        self.idx = 0
        self.pos_s = 0.0
        self.pending = None
        self.pending_t = 0.0
        self.pending_retries = 0
        self.drawn = None
        self.last_frame_obj = None
        self._next_tick = 0.0
        self._eof_t = 0.0              # 开始读不到帧的时刻（等归档追上用）

        # 标记
        # {'t_s','wall','wall_est','pre_s','note','session','seg'}
        # wall_est=True 表示该标记由实时估算落点（直播延迟补偿），可能有 ±1s 误差
        self._marks = []
        self._mark_seq = 0
        self._marks_path = os.path.join(args.out_dir, 'marks.jsonl')
        # 「打开录像」：从本机目录打开的一场历史录像。
        # {'name','root','dir','segs','base'} / None = 跟着直播那场走。
        # 打开期间直播照常收（小窗仍在动），只是回放的数据源换成这份快照。
        self.opened = None
        # 启动时 Archive 还没轮询到场次名（session 为空），此刻加载不了。
        # 等第一次拿到场次名再读，见 _maybe_load_marks()。
        self._marks_loaded = False
        self._marks_loaded_for = ''      # 已载入的标记属于哪一场
        self._mark_log_idx = {}          # 标记 → 它在各源文件里的起始行号
        self._mark_log_lines = {}        # 源文件 → 原始行（删除标记时重写用）
        # 直播延迟：屏幕这一刻 → RK3588 此刻拍到的画面，共差多少秒。
        # = 传输+解码（实测，feed.transport_lat） + 分发队列积压（对表测得，
        # 保存在 _lat_extra）。后者画面静帧看不出来，只能靠跟表/场景叠帧。
        self._live_lat = 0.8
        self._lat_frozen_t = 0.0       # 画面最近一次变动的本机时刻
        self._lat_frozen_n = -1        # 当时对应的 feed.image_n
        # 时间码用的录制轴位置：只在画面/时间头给出**更新**值时才往前走。
        # 断流时停住、重连时由 LiveFeed 补偏移，界面才不会来回跳。
        self._tc_pos = None
        self._tc_frame = -1            # 上次 timecode() 时的 feed.image_n
        self._jump_t = None            # 待跳转的回放时刻（主循环里执行）
        self._lat_extra = 0.0          # 对时测得的分发/冻结余量
        self._lead = 0.0               # 裁判看到的比听到哨声多滞后（标记自动多提前）
        self._last_feed_t = 0.0
        self._pending_note = ''

        self._quit = False
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self):
        self.feed.start()
        self.archive.start()
        self.loader.start()
        # 启动时若 RK3588 已在录制，标记为录制中
        threading.Thread(target=self._detect_recording,
                         daemon=True).start()

    def request_quit(self):
        self._quit = True

    def shutdown(self):
        self.feed.stop()
        self.archive.stop_evt.set()
        self.loader.stop()
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def quit_requested(self):
        return self._quit

    # ---------- 对外状态（供 UI 读取）----------
    def state(self):
        now = time.monotonic()
        clk = self.clock_offset + (now - self.clock_t0)
        self._maybe_load_marks()
        return {
            # 打开的是历史录像时，UI 据此把顶栏标成「历史场次」、
            # 亮出退出入口，并禁用录制/直播相关按钮
            'history': self.opened is not None,
            'history_root': self.history_root(),
            'marks_file': self.marks_file(),
            'live_ok': self.feed.alive(),
            'live_connecting': self.feed.dec is not None
                               and not self.feed.alive(),
            'live_fps': self.feed.fps(),
            'recording': self.recording,
            'remote_busy': self.remote_busy,
            'mode': self.mode,
            'playing': (self.mode == 'replay' and self.cap is not None
                        and not self.paused and not self.ended),
            'speed_idx': self.speed_idx,
            'clock': fmt_time(clk),
            'session': self._session_label(),
            'session_len': fmt_time(self.timeline_end()),
            'status': self._status_text(),
            # 归档
            'archive_busy': self.archive_busy,
            'archive_done': self.archive_done,
            'archive_total': self.archive_total,
            'archive_phase': self.archive_phase,
            'archive_session': self.archive_session,
            'archive_dir': self.archive_dir,
        }

    def _session_label(self):
        """当前录制会话（一场比赛）的简短标识"""
        if self.opened is not None:
            base = self.opened['name']
            if base.startswith('stage1_') and len(base) >= 22:
                return '历史 %s-%s %s:%s' % (
                    base[11:13], base[13:15], base[16:18], base[18:20])
            return '历史 ' + base
        name = self.archive.session_name()
        if not name:
            return '本场 —'
        base = os.path.basename(name.rstrip('/'))
        # stage1_20261031_214312 → 10-31 21:43
        if base.startswith('stage1_') and len(base) >= 22:
            # base[7:11]=2026, [11:13]=10, [13:15]=31, [16:18]=21, [18:20]=43
            return '本场 %s-%s %s:%s' % (
                base[11:13], base[13:15], base[16:18], base[18:20])
        return '本场 ' + base

    def _status_text(self):
        parts = []
        alarm = self.archive.camera_alarm
        if alarm:
            parts.append('⚠ 相机无画面 %s 秒（上次错误码 0x%08x）—— 请检查/重插 USB'
                         % (alarm.get('seconds_stuck', '?'),
                            alarm.get('last_ret', 0)))
        if self.msg and time.monotonic() - self.msg_t < 6.0:
            parts.append(self.msg)
        e = self.archive.error
        if e:
            parts.append('归档: %s' % e)
        ld = self.loader.loading_name()
        if ld:
            n = self.loader.failures(ld)
            if n:
                parts.append('重试中(%d): %s' % (n, ld))
            else:
                parts.append('加载中: %s' % ld)
        if self.feed.status:
            lat = self.live_latency()
            parts.append('直播: %s (%s)%s' % (
                self.feed.status, self.feed.decoder,
                '' if lat is None else '  延迟 %.1fs' % lat))
        if not parts:
            parts.append('就绪')
        return '\n'.join(parts)

    def current_frame(self):
        """当前应显示的主画面帧

        注意：直播帧绝不写进 self.drawn——那是"回放最后一帧"的槽位。
        误写会让切回放的头几秒（还在拉片段）显示残存的直播画面，误导用户。

        回放模式下：切段/首次加载期间返回 None，让 UI 显示"正在准备片段"，
        而不是继续挂着上一段（或直播）的残留画面。
        """
        if self.mode == 'live':
            img = self.feed.image
            self.last_frame_obj = img
            return img
        # 首次进入回放：还没有任何回放帧可显示，清屏等加载。
        # 跨段续播（self.drawn 已有回放帧）则保留上一帧，避免闪黑。
        if self.pending is not None and self.drawn is None:
            self.last_frame_obj = None
            return None
        self.last_frame_obj = self.drawn
        return self.drawn

    def live_frame(self):
        return self.feed.image

    def live_fps(self):
        return self.feed.fps()

    def placeholder_text(self):
        if self.mode == 'live':
            if self.feed.dec is None and not self.feed.status == 'off':
                return '直播连接中…'
            return '直播：等待 RK3588 画面'
        if self.pending is not None:
            name = self.pending[0]['file']
            n = self.loader.failures(name)
            if n:
                return '回放：准备片段失败（第 %d 次）…' % n
            return '回放：正在准备片段…'
        return '回放：无画面'

    def _local_fallback(self):
        """远端没有录制场次时，看本机归档里有没有可回放的一场。

        归档勾了「删除 RK3588 原文件」之后，RK3588 上一个场次都不剩，
        此时回放只能读本机归档；否则时间轴空白、点了也放不出来。
        只在远端**完全空**时兜底，避免和正在录的场次混在一起。
        （这一段与「打开录像」无关，那边是手动选的，见 open_session。）
        """
        if self.archive.segments_available():
            return []
        arch = self.archiver
        try:
            newest = arch.local_sessions(self.archive_dir)[:1]
        except OSError:
            return []
        if not newest:
            return []
        name = newest[0]
        if self._local_fb_name == name:
            return self._local_fb_segs
        self._local_fb_segs = arch.load_local(name, self.archive_dir)
        self._local_fb_name = name
        return self._local_fb_segs

    def _live_edge_s(self):
        """Last observed live position: advance with fresh video, freeze on loss.

        Keep this display edge separate from sealed segments used by seek().
        A new recording session starts a new clock.
        """
        session = self.archive.session_name()
        if session != getattr(self, '_display_clock_session', None):
            self._display_clock_session = session
            self._display_live_edge = 0.0
        edge = getattr(self, '_display_live_edge', 0.0)
        if self.feed.alive():
            pos = self.feed.live_pos()
            if pos is None:
                pos = self._lat_show()
            if pos is not None:
                edge = max(edge, max(0.0, pos))
                self._display_live_edge = edge
        return edge

    def timeline_end(self):
        if self.opened is not None:
            segs = self.opened['segs']
            return segs[-1]['end_s'] if segs else 0.0
        segs = self._local_fallback()
        if segs:
            return segs[-1]['end_s']
        # Sealing occurs every ~10s; the visible duration must track live video.
        # Replay availability still comes exclusively from segments()/seek().
        return max(self.archive.last_end(), self._live_edge_s())

    def segments(self):
        if self.opened is not None:
            return self.opened['segs']
        return self._local_fallback() or self.archive.snapshot()

    def in_history(self):
        """当前回放的是「打开」的历史录像，而不是正跟着直播那场。"""
        return self.opened is not None

    def history_root(self):
        return self.opened['root'] if self.opened is not None else None

    def segments_window(self, span_s=None):
        """默认覆盖整场；显式传 span_s 时才限制为末尾时间窗。"""
        segs = self.segments()
        if not segs or span_s is None:
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
        # 看历史录像时直播仍在跑，但它的时间轴是另一场，不能拿来当播放头
        if self.mode == 'replay' or self.opened is not None:
            return self.pos_s
        return self.timeline_end()

    def current_seg_index(self):
        if self.seg is None:
            return -1
        for i, s in enumerate(self.segments()):
            if s['file'] == self.seg['file']:
                return i
        return -1

    # ---------- 直播 / 回放切换 ----------
    def to_live(self):
        """回到直播画面。开了历史录像时，先断开那份再回直播。"""
        if self.opened is not None:
            self.close_session()
        else:
            self.mode = 'live'
            self.paused = False
            self.ended = False
            self.pending = None
            self.drawn = None
            self.last_frame_obj = None
            self.pos_s = self.timeline_end()
        self.feed.set_want(True)
        self._say('切换到直播')

    def to_replay(self):
        segs = self.segments()
        if not segs:
            self._say('还没有已封存的片段')
            return
        self.mode = 'replay'
        # 清掉上一轮回放/直播残留帧，否则拉片段的几秒会显示错误画面
        self.drawn = None
        self.last_frame_obj = None
        # 回放最后一帧：末段刚封存（常是空/截断文件），从它的开头起播最稳。
        # 往前多留 0.5s 容差，避免落在段边界外被 seek 归到坏段上。
        target = segs[-1]
        pos = target['start_s'] + 0.5
        if pos >= target['end_s']:
            pos = target['start_s']
        self._say('正在加载回放片段…')
        self.seek(pos)

    # ---------- 录像库 ----------
    def _load_library_roots(self):
        """读回上次记住的额外录像库目录（不存在/不是目录的直接跳过）。"""
        try:
            with open(os.path.join(self.args.out_dir,
                                   'library_roots.json')) as f:
                dirs = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(dirs, list):
            return
        for d in dirs:
            if isinstance(d, str) and os.path.isdir(d) \
                    and d not in self.library.roots:
                self.library.roots.append(d)

    def library_roots(self):
        """录像库扫描的根目录（新的在前）。第一个用于 UI 顶栏提示。"""
        return list(self.library.roots)

    def add_library_root(self, path):
        """把用户选的目录加入录像库（本会话有效，同时记住供下次启动）。

        归档目录永远是第一个 root；额外目录追加在后面，重复的忽略。
        """
        d = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(d) or d in self.library.roots:
            return
        self.library.roots.append(d)
        try:
            with open(os.path.join(self.args.out_dir,
                                   'library_roots.json'), 'w') as f:
                json.dump([r for r in self.library.roots
                           if r != self.archive_dir], f)
        except OSError:
            pass

    # ---------- 打开历史录像 ----------
    def open_session(self, path):
        """打开本机任意位置的一场录像（「打开录像」对话框选的目录）。

        只读目录、换数据源，不碰 RK3588：直播照常收（小窗还会动），
        录制中也不会被打断。标记改写到该场自己的目录里，不再和当前场
        记进同一个文件。返回场次名；打不开返回 None。
        """
        r = svc.read_session_dir(path)
        if r is None:
            self._say('打不开：%s 不是完整的场次目录（缺 meta.jsonl 或分段）'
                      % path)
            return None
        name, segs, base = r
        d = os.path.abspath(os.path.expanduser(path))
        root = os.path.dirname(d)
        if self.opened is not None:
            self.close_session(quiet=True)
        self.opened = {'name': name, 'root': root, 'dir': d,
                       'segs': segs, 'base': base}
        # 换个场次 = 换一套标记：旧列表立刻清掉，_refresh_mark_list 的签名
        # 也跟着变，侧栏不会还挂着上一场的内容。
        self._marks = []
        self._mark_seq = 0
        self._load_marks(name)
        self.mode = 'replay'
        self.paused = False
        self.ended = False
        self.pending = None
        self.seg = None
        self.pos_s = 0.0
        self.drawn = None
        self.last_frame_obj = None
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self._jump_t = None
        self._say('已打开 %s（%d 段%s）' % (
            name, len(segs), '' if base is not None else '，无绝对时间锚点'))
        self.seek(segs[0]['start_s'] + 0.5)
        return name

    def close_session(self, quiet=False):
        """退出历史录像，回到直播那场。"""
        if self.opened is None:
            return
        self.opened = None
        self._marks = []
        self._mark_seq = 0
        self._marks_loaded = False
        self._maybe_load_marks()
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.seg = None
        self.pending = None
        self.drawn = None
        self.last_frame_obj = None
        self.mode = 'live'
        self.paused = False
        self.ended = False
        self.pos_s = self.timeline_end()
        if not quiet:
            self._say('已退出历史录像，回到直播')

    def seek(self, pos_s, check_range=True):
        segs = self.segments()
        if not segs:
            self._say('还没有已封存的片段')
            return
        end = segs[-1]['end_s']
        # 超出已封存范围 = 还在直播流里：别立刻播放旧画面（用户按的是
        # 「刚吹的哨」），记下来等封存后由 tick 再跳
        if check_range and pos_s > end - 0.05:
            self._jump_t = pos_s
            self._say('画面还在直播流里，封存后自动跳到 %.0fs' % pos_s)
            return
        pos_s = max(0.0, pos_s)
        seg = None
        for s in segs:
            if s['start_s'] <= pos_s < s['end_s']:
                seg = s
                break
        if seg is None:
            seg = segs[-1]
            pos_s = max(0.0, end - 0.1)
        # 打开的是外部目录：绝不去远端 scp（那目录只有本机有），
        # 直接告诉加载器按这个绝对路径取原文件。不依赖 self.opened 是否
        # null——同一场录像点「回放」时未必是「打开录像」进来的。
        seg = dict(seg, local_dir=os.path.dirname(seg['dir']))
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
                # 段本身损坏：别在这段上反复试，往前退到最近一个完好段
                if err.startswith('SegmentCorrupt'):
                    bad_name = seg['file']
                    self.loader.clear_error(bad_name)
                    self.loader.mark_bad(bad_name)
                    alt = self._prev_ok_seg(seg)
                    if alt is not None:
                        self._say('片段 %s 已损坏，改放 %s'
                                  % (bad_name, alt['file']))
                        self.seek(alt['start_s'])
                        return
                    self._say('片段 %s 已损坏，且无更早可用片段' % bad_name)
                    return
                # 网络类失败：清掉错误、重新请求，给一次自愈机会
                self.loader.clear_error(seg['file'])
                self._say('重新拉取 %s（上次: %s）' % (seg['file'], err))
            self.pending = (seg, pos_s)
            self.pending_t = time.monotonic()
            self.pending_retries = 1
            self.seg = seg              # 立即反映目标段，播放头/高亮正确
            self.pos_s = pos_s
            self._say('准备片段 %s …' % seg['file'])
            self.loader.request(seg)
            return
        self._open_seg(seg, info, pos_s)

    def preview_seek(self, pos_s):
        """拖动时间轴时只更新位置显示，松手才真正 seek"""
        self.pos_s = pos_s

    def _jump(self, pos_s):
        if self.cap is None:
            return
        f = int(round((pos_s - self.seg['start_s']) * self.fps))
        f = max(0, min(self.frame_count - 1, f))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        self._next_tick = 0.0      # seek 后重新对齐时钟，否则会瞬间补帧跳过头
        self._eof_t = 0.0
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx = f + 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
            self.ended = False

    def _open_seg(self, seg, info, pos_s):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        cap = self._open_capture(info)
        if not cap.isOpened():
            self._say('打开回放失败: %s' % info['mkv'])
            return
        self.cap = cap
        self.seg = seg
        # MKV 头里的 fps 是 remux 时写的标称 59.94，而录制端实际是 59.2；
        # 用容器值换算位置会和 PTS 慢慢错开，故以录制端实测值为准。
        self.fps = info['fps'] or cap.get(cv2.CAP_PROP_FPS) or 59.2
        self.frame_count = (int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                            or info['frames'])
        self.idx = 0
        self.pending = None
        self._next_tick = 0.0
        self._jump(pos_s)
        self._say('就绪 %s  %d 帧' % (seg['file'], self.frame_count))
        nxt = self._next_seg(seg)
        if nxt is not None:
            self.loader.request(nxt, idle_only=True)

    def _open_capture(self, info):
        return cv2.VideoCapture(info['mkv'])

    def _safe_seek_target(self, t):
        """把跳转落点挪离段尾：落在段最后 0.5s 内时回退到段起点后 1s。

        段尾帧可疑（录制端还在写、remux 可能截断），跳进去容易读到 EOF
        被判成"播放结束"而冻结画面。宁可早几秒也不能卡住。
        """
        for s in self.segments():
            if s['start_s'] <= t < s['end_s']:
                if t > s['end_s'] - 0.5:
                    return min(t, s['start_s'] + 1.0)
                return t
        return t

    def _next_seg(self, seg):
        for s in self.segments():
            if s['file'] != seg['file'] and s['start_s'] >= seg['end_s'] - 0.01:
                return s
        return None

    def _prev_ok_seg(self, seg):
        """往前找最近一个未损坏的段（当前段坏了时回退用）"""
        prev = None
        for s in self.segments():
            if s['start_s'] >= seg['start_s'] - 0.01:
                break
            if not self.loader.is_bad(s['file']):
                prev = s
        return prev

    def tick(self):
        """回放推进；返回下一帧应等待的秒数（顺带采集直播延迟样本）"""
        if self.mode != 'replay':
            self._sample_latency()
            if self._jump_t is not None:
                # 等那段封存进 meta 后再跳，否则跳到的还是直播画面。
                # 不能等"分不够新"的提前量：归档轮询本身有延迟，
                # 只要求它确实开始覆盖目标时刻（留 0.3s 容差）。
                if self.archive.last_end() > self._jump_t + 0.3:
                    # 段末尾几帧常是截断的（remux 还没收尾），落进去会
                    # 立刻 read 失败、被当成播放结束而冻结。往前挪一点。
                    t = self._safe_seek_target(self._jump_t)
                    self._jump_t = None
                    self._say('加载现场回放…')
                    self.seek(t, check_range=False)
            return None
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
                    self._say('回放加载失败: %s' % err)
                elif (time.monotonic() - self.pending_t > 8.0
                      and self.loader.loading_name() == ''):
                    # 拉取已结束却没有结果：请求可能丢了，重试有限次
                    if self.pending_retries < 3:
                        self.pending_retries += 1
                        self.pending_t = time.monotonic()
                        self._say('重新准备片段 %s …' % seg['file'])
                        self.loader.request(seg)
                    else:
                        self.pending = None
                        self._say('回放加载超时: %s' % seg['file'])
            return None
        if self.cap is None or self.paused or self.ended:
            return None
        # 按墙钟补帧：每次 tick 只读一帧会让回放速度被 UI 刷新率锁死
        # （主循环最快 33ms≈30fps，59.94fps 的片子只能放一半速度）。
        # 这里按目标时间戳决定该读几帧，落后就补，最多补 6 帧防雪崩。
        now = time.perf_counter()
        step = 1.0 / (self.fps * self._speed())
        if self._next_tick <= 0:
            self._next_tick = now
        due = int((now - self._next_tick) / step) + 1
        due = max(1, min(6, due))
        ok = False
        frame = None
        for _ in range(due):
            ok, frame = self.cap.read()
            if not ok:
                break
            self.idx += 1
        if ok:
            self._eof_t = 0.0
            self.drawn = self._fit(frame)
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
            self._next_tick += due * step
            if self._next_tick < now - step * 6:   # 卡太久就重新对齐，别追债
                self._next_tick = now
        else:
            nxt = self._next_seg(self.seg)
            # 跳过已确认损坏的段，避免在空/截断片段上原地卡死
            while nxt is not None and self.loader.is_bad(nxt['file']):
                self._say('跳过损坏片段 %s' % nxt['file'])
                nxt = self._next_seg(nxt)
            if nxt is None:
                # 本地读完不代表真没内容：archive 轮询可能还没发现后续段
                # （尤其跳到刚封存的那段时）。archive 已推进到本段之后
                # 却仍找不到后继，或等太久，才算真的播完。
                if self._eof_t <= 0.0:
                    self._eof_t = now
                # 打开的历史录像整份都在本机：不会再有后续段，读到末尾
                # 立刻算播完；只有跟着直播那场时才要等 archive 追上。
                done = (self.opened is not None
                        or self.archive.last_end() > self.seg['end_s'] + 1.0
                        or now - self._eof_t > 12.0)
                if done:
                    self.ended = True
                    self.paused = True
                    self._eof_t = 0.0
            else:
                info = self.loader.clip(nxt['file'])
                if info is not None:
                    self._open_seg(nxt, info, nxt['start_s'])
                else:
                    self.pending = (nxt, nxt['start_s'])
                    self.loader.request(nxt, idle_only=self.opened is not None)
            return None
        # 返回距下一个播放时刻还剩多久，可能为负（落后了要立刻再来一次）
        return self._next_tick - time.perf_counter()

    def _fit(self, img):
        w = self.args.display_width
        if not w or img.shape[1] == w:
            return img
        h = int(round(img.shape[0] * w / img.shape[1]))
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    def _speed(self):
        return (1.0, 0.5, 0.25)[self.speed_idx]

    # ---------- 回放控制 ----------
    def toggle_play(self):
        if self.mode != 'replay' or self.cap is None:
            return
        if self.ended:
            self.paused = False
            self.ended = False
            self._jump(self.seg['start_s'])
        else:
            self.paused = not self.paused

    def step(self, delta):
        if self.mode != 'replay' or self.cap is None:
            return
        self.paused = True
        self.ended = False
        f = max(0, min(self.frame_count - 1, self.idx - 1 + delta))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        self._next_tick = 0.0
        ok, frame = self.cap.read()
        if ok:
            self.drawn = self._fit(frame)
            self.idx = f + 1
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
        else:
            # 落点读不到（多因段尾巴被截断）：别判死，让 tick 去接后继段
            self.idx = max(0, f - 1)
            self.pos_s = self.seg['start_s'] + self.idx / self.fps
            self.ended = False

    def set_speed(self, idx):
        self.speed_idx = idx
        self._say('速度 %.2fx' % self._speed())

    def jump_to_segment(self, i):
        segs = self.segments()
        if 0 <= i < len(segs):
            self.seek(segs[i]['start_s'])

    # ---------- 标记 ----------
    def mark_session(self):
        """当前标记归属哪一场。

        打开历史录像时就是那一场；否则跟随 RK3588 上正在录/最新的场次。
        """
        if self.opened is not None:
            return self.opened['name']
        name = self.archive.session_name()
        return os.path.basename(name.rstrip('/')) if name else ''

    def mark_session_dir(self):
        """这一场标记该写进哪个目录：有归档副本就写副本，否则写总表。

        归档目录里已有本场（停止录制后拷回来了）时，标记写进场次目录
        自己的 marks.jsonl——录像拷走时标记跟着走，不会和别的场次混在
        一个文件里。还没归档的场次仍写 replay/marks.jsonl（按 session
        字段区分），归档时再合并进去。
        """
        if self.opened is not None:
            return self.opened['dir']
        name = self.mark_session()
        if name:
            d = os.path.join(self.archive_dir, name)
            if os.path.isdir(d):
                return d
        return None

    def _marks_file(self, dir_path):
        return (os.path.join(dir_path, 'marks.jsonl') if dir_path
                else self._marks_path)

    def marks_file(self):
        """供 UI/导出显示：本场标记实际落在哪个文件。"""
        return self._marks_file(self.mark_session_dir())

    def _maybe_load_marks(self):
        """拿到场次名后读一次本场标记；没有场次名就继续等。

        场次名一旦变了（RK3588 开了新的一场）必须重读：以前只读一次，
        上一场的标记就一直挂在侧栏上。
        """
        cur = self.mark_session()
        if not cur:
            return
        if self._marks_loaded and cur == self._marks_loaded_for:
            return
        self._marks_loaded = True
        self._marks_loaded_for = cur
        self._load_marks()

    def _load_marks(self, session=None):
        """把本场已落盘的标记读回来；session 为空 = 打开历史录像那一场。

        优先读场次目录自己的 marks.jsonl；没有（旧场次/尚未归档）再读
        总表 replay/marks.jsonl 里属于这一场的记录。
        """
        import json
        if session is None:
            session = self.mark_session()
        cur = session
        if not cur:
            return
        d = self.mark_session_dir()
        paths = [self._marks_file(d)]
        if d is not None and self._marks_path not in paths:
            paths.append(self._marks_path)     # 旧数据还留在总表里
        # (来源文件, 行)：来源要跟着行一起走——同一事件可能在总表和场次
        # 目录里各有一份，撤掉总表里那份时得把那两行也撤掉，不能只删事件。
        lines = []
        for p in paths:
            try:
                with open(p, encoding='utf-8') as f:
                    lines.extend((p, x) for x in f.read().splitlines())
            except OSError:
                continue
        dropped = 0
        by_key = {}
        order = []
        idx = {}                 # key → 该事件在各自来源文件里的起始行号
        for lineno, (src, ln) in enumerate(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(d, dict) or 't_s' not in d:
                continue
            # 只认本场的标记。**没写 session 的来自总表 = 早先的版本遗留，
            # 一律丢弃**（放行过它们，结果几天前的旧标记全混进当前场次）。
            # 场次目录自己的 marks.jsonl 例外：整份都属于这一场，可以不写
            # session。
            if d.get('session', cur if src != self._marks_path else '') != cur:
                dropped += 1
                continue
            k = (d.get('wall'), d.get('pre_s'), d.get('note', '').split(' ')[0])
            if d.get('op') in ('note', 'shift'):
                base = by_key.get(k)
                if base is not None:
                    if d['op'] == 'note':
                        base['note'] = d['note']
                    else:
                        base['t_s'] = d['t_s']
                        for f2 in ('clock', 'frame', 'wall', 'wall_local'):
                            if f2 in d:
                                base[f2] = d[f2]
                        idx.setdefault(k, {})[src] = lineno
                continue
            if k in by_key:
                continue
            idx[k] = {src: lineno}
            by_key[k] = d
            order.append(k)
        # 记下每条标记在源文件里的起始行：追加式日志里删掉它（含后续的
        # 备注/平移行）只能按文件重写，见 _drop_marks_from_logs()。
        self._mark_log_idx = {k: idx.get(k, {}) for k in order}
        self._mark_log_lines = {p: [x for (s, x) in lines if s == p]
                                for p in paths}
        self._marks = [by_key[k] for k in order]
        self._mark_seq = len(self._marks)
        if dropped:
            self._say('已载入本场标记 %d 条（略过 %d 条非本场）'
                      % (len(self._marks), dropped))

    def add_mark(self, pre_s=0.0, est=False, note=None):
        """记录判罚标记。

        pre_s>0 时记录的位置提前 pre_s 秒（"吹哨点"），用于直播下按哨声标记：
        因为直播画面本身滞后，要标的是"哨响那一刻"而不是"那时看到的画面"。
        est=True 表示直播实时标记，位置由延迟补偿估算得出。
        """
        if self.mode == 'replay':
            base_pos = self.pos_s
        else:
            # 直播：按"此刻真实拍到的画面"标，不受分发队列积压影响。
            # 优先用画面帧的精确位置（与时间码同源）；没拿到时再按墙钟估。
            base_pos = self.feed.live_pos()
            if base_pos is None:
                base_pos = self._lat_show()
        t = max(0.0, base_pos - pre_s - self._lead)
        self._mark_seq += 1
        self._pending_note = note or '标记 %d' % self._mark_seq
        wall = self.wall_at(t)
        m = {'t_s': round(t, 3),
             'clock': self.clock_at(t),
             'frame': int(round(t * self.fps)),
             'wall_est': bool(est or pre_s > 0),
             'pre_s': pre_s,
             'session': self.mark_session(),
             'seg': (self.seg['file'] if self.mode == 'replay'
                     and self.seg is not None else ''),
             'clip': (0.0 if self.mode != 'replay'
                      else round(max(0.0, self.pos_s - self.seg['start_s']), 3)
                      if self.seg is not None else 0.0),
             'note': self._pending_note}
        if wall is not None:
            m['wall'] = round(wall, 3)
            m['wall_local'] = fmt_hms(wall)
        self._marks.append(m)
        self._persist_mark(m)
        self._say('已标记 %s @ %s%s' % (m['note'], fmt_time(t),
                                       '  (估)' if m['wall_est'] else ''))
        return m

    def jump_to_mark(self, i):
        """跳到第 i 条标记；画面还在直播流里传时就等到了再跳。"""
        marks = self._marks
        if not (0 <= i < len(marks)):
            return
        t = marks[i]['t_s']
        if self.opened is not None:
            # 历史录像整份都在本机，不存在"还在直播流里"这一段
            self.seek(t)
            return
        end = self.archive.last_end()
        if t >= end - 0.2:
            self._jump_t = t
            self._say('该标记还在直播流里，封存后自动跳转…')
            return
        self.seek(t)

    def remove_mark(self, i):
        """删掉第 i 条标记（误按/上一场残留/重复标记用）。

        日志是追加式的：删一条 = 在它所在的源文件里去掉起始行和它后面
        紧跟着的备注/平移行，其余行原样保留再整体重写。
        """
        marks = self._marks
        if not (0 <= i < len(marks)):
            return False
        k = (marks[i].get('wall'), marks[i].get('pre_s'),
             marks[i].get('note', '').split(' ')[0])
        idx = getattr(self, '_mark_log_idx', {}).get(k) or {}
        bufs = getattr(self, '_mark_log_lines', {})
        for src, start in idx.items():
            rows = bufs.get(src)
            if not rows or not (0 <= start < len(rows)):
                continue
            end = start + 1
            # 后面属于同一事件的 op 行一起删（key 字段一模一样）
            while end < len(rows):
                try:
                    d2 = json.loads(rows[end])
                except ValueError:
                    end += 1
                    continue
                if d2.get('op') in ('note', 'shift') and (
                        d2.get('wall'), d2.get('pre_s'),
                        d2.get('note', '').split(' ')[0]) == k:
                    end += 1
                else:
                    break
            rows[start:end] = [None] * (end - start)
        for src, rows in bufs.items():
            if not any(r is None for r in rows):
                continue
            try:
                keep = [r for r in rows if r is not None]
                with open(src, 'w', encoding='utf-8') as f:
                    f.write(''.join(r + '\n' for r in keep))
            except OSError as e:
                self._say('删除标记写盘失败: %s' % e)
                return False
        note = marks[i].get('note', '')
        del self._marks[i]
        self._say('已删除标记 %s' % note)
        return True

    def mark_note(self, text):
        """给最近一条标记补充备注（如"#12 推人"）"""
        text = (text or '').strip()
        if not text or not self._marks:
            return
        m = self._marks[-1]
        prefix = m['note'].split('  ')[0]    # 同一标记只保留最后一次备注
        m['note'] = '%s  %s' % (prefix, text)
        self._persist_mark(dict(m, op='note'))
        self._say('备注已记录: %s' % text)

    def note_mark(self, i, text):
        """给第 i 条标记补充备注。平板遥控用，与 mark_note 同一套落盘。"""
        text = (text or '').strip()
        if not text or not (0 <= i < len(self._marks)):
            return False
        m = self._marks[i]
        prefix = m['note'].split('  ')[0]
        m['note'] = '%s  %s' % (prefix, text)
        self._persist_mark(dict(m, op='note'))
        self._say('备注已记录: %s' % text)
        return True

    def fix_marks(self, delta_s):
        """标记本机时刻按 delta_s 平移（人为±N 秒修正）"""
        if not self._marks:
            self._say('还没有标记')
            return 0
        for m in self._marks:
            m['t_s'] = max(0.0, round(m['t_s'] + delta_s, 3))
            m['clock'] = self.clock_at(m['t_s'])
            m['frame'] = int(round(m['t_s'] * self.fps))
            w = self.wall_at(m['t_s'])
            if w is not None:
                m['wall'] = round(w, 3)
                m['wall_local'] = fmt_hms(w)
            self._persist_mark(dict(m, op='shift'))
        self._lead = round(self._lead + delta_s, 3)
        self._say('%d 条标记已平移 %+.1fs' % (len(self._marks), delta_s))
        return self._lead

    def note_broken(self):
        """“屏幕上迟了多久才看到”修正：标记时画面若还停在哨响那一刻，
        说明比标的那一下又晚到一截，把多等的这段补进延迟。"""
        if self.mode != 'live' or not self.feed.alive():
            return None
        pts = self.feed.image_pts
        if pts is None:
            return None
        # 屏幕上那帧是 pts 时刻拍的、之后过了传输时间才显示；
        # 现在离它"拍下"过了多久，超出传输的部分就是画面停住多等的
        frozen = time.time() - self.pts_wall(pts) - (
            self.feed.transport_lat or 0.0)
        if not (0.25 <= frozen <= 15.0):
            self._say('当前画面就比标的时刻新，不用修正')
            return None
        lat = self.lat_fix(frozen)
        self._say('延迟已修正 +%.1fs → 共 %.1fs' % (frozen, lat))
        return lat

    def pts_wall(self, pts_s):
        """录制轴秒数 → 画面上实际是几点几分（绝对 epoch 秒）"""
        base = self.archive.base_ms()
        return None if base is None else base / 1000.0 + pts_s

    def export_marks(self):
        """把标记表导出成 CSV（含绝对时间/本场时钟/帧号），便于赛后复核"""
        try:
            os.makedirs(self.args.out_dir, exist_ok=True)
            path = os.path.join(
                self.args.out_dir,
                time.strftime('marks_%Y%m%d_%H%M%S.csv'))
            with open(path, 'w', encoding='utf-8-sig') as f:
                f.write('序号,绝对时间,本场时钟,帧号,估算,提前秒,备注,会话\n')
                for i, m in enumerate(self._marks, 1):
                    f.write('%d,%s,%s,%d,%s,%.1f,%s,%s\n' % (
                        i, m.get('wall_local', ''), m.get('clock', ''),
                        m.get('frame', 0), '是' if m.get('wall_est') else '否',
                        m.get('pre_s', 0.0),
                        str(m.get('note', '')).replace(',', '，'),
                        m.get('session', '')))
            self._say('标记已导出: %s' % path)
            return path
        except OSError as e:
            self._say('导出失败: %s' % e)
            return None

    def _persist_mark(self, m):
        """写进本场自己的 marks.jsonl（也许在归档目录里）。

        每次现算路径而不是缓存：录制中途归档完成，本场目录才刚出现，
        之后的标记就该跟着写进去。
        """
        try:
            path = self.marks_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            line = json.dumps(m, ensure_ascii=False)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            # 同步进内存副本：不然"刚记的这条"不在 _mark_log_lines 里，
            # 紧接着删它就会漏删磁盘上的那一行
            bufs = getattr(self, '_mark_log_lines', None)
            if bufs is not None:
                bufs.setdefault(path, []).append(line)
                k = (m.get('wall'), m.get('pre_s'),
                     m.get('note', '').split(' ')[0])
                idx = getattr(self, '_mark_log_idx', None)
                if idx is not None and m.get('op') not in ('note', 'shift'):
                    idx[k] = {path: len(bufs[path]) - 1}
        except OSError:
            pass

    # ---------- 截图 ----------
    def snapshot(self):
        img = self.drawn if self.mode == 'replay' else self.feed.image
        if img is None:
            self._say('无画面可截图')
            return
        os.makedirs(self.args.out_dir, exist_ok=True)
        path = os.path.join(
            self.args.out_dir, time.strftime('shot_%Y%m%d_%H%M%S.jpg'))
        cv2.imwrite(path, img)
        self._say('截图: %s' % path)

    # ---------- 绝对时间码 ----------
    def base_ms(self):
        """当前这一场的录制起点（绝对 UTC 毫秒），未知返回 None。

        打开历史录像时用那份 meta 里算出来的 base；否则跟随直播那场。
        """
        if self.opened is not None:
            return self.opened['base']
        return self.archive.base_ms()

    def wall_at(self, pos_s):
        """录制秒数 → 绝对 epoch 秒；本场起点未知返回 None"""
        base = self.base_ms()
        if base is None:
            return None
        return base / 1000.0 + pos_s

    def clock_at(self, pos_s):
        """录制秒数 → 本场时钟 HH:MM:SS（即录制时长）"""
        return fmt_clock(pos_s)

    def now_pos(self):
        """当前画面对应的录制轴秒数（直播 = 屏幕上这帧的精确时刻）

        直播下只**前进不后退**：断流时画面停在最后一帧，位置也停住；
        重连后服务端 PTS 归零，live_pos 里已补上续接偏移，不会跳回开头。
        """
        if self.mode != 'live':
            return self.pos_s
        pos = self.feed.live_pos()
        if pos is not None:
            if self._tc_pos is None or pos > self._tc_pos:
                self._tc_pos = pos
        if self._tc_pos is not None:
            return self._tc_pos
        return self._lat_show()      # 还没拿到过 PTS：开场先按估算走

    def feed_changed(self):
        """上次 timecode() 之后画面有没有换新帧（UI 据此延迟刷新）"""
        return self._tc_frame != self.feed.image_n

    def _tc_snapshot(self):
        """一次算出这一轮要显示的三个值，三者必须同源：
        以前时钟取一帧的 PTS、帧号取另一帧的，上下两行会差一两帧。"""
        self._tc_pos = None          # 重算一遍，下面的 now_pos 会缓存
        pos = self.now_pos()
        fr = int(pos * self.fps)
        if self.mode == 'replay' and self.seg is not None:
            fr = int(round((self.pos_s - self.seg['start_s']) * self.fps
                           + self.idx - 1))
        self._tc_frame = self.feed.image_n
        return pos, fr


    def frame_no(self):
        """回放当前帧在本场内的绝对帧号（跨段连续）"""
        if self.mode != 'replay' or self.seg is None:
            n = self.now_pos() * self.fps
            return int(n)
        return int(round((self.pos_s - self.seg['start_s']) * self.fps
                         + self.idx - 1))

    def timecode(self):
        """当前画面时间码：绝对时钟 + 本场时钟 + 帧号（三者同一帧）"""
        pos, fr = self._tc_snapshot()
        wall = self.wall_at(pos)
        clk = self.clock_at(pos)
        if wall is None:
            return '%s  #%d' % (clk, fr)
        return '%s  %s  #%d' % (fmt_hms(wall), clk, fr)

    # ---------- 直播延迟与对时 ----------
    # 直播画面相对"RK3588 此刻拍到的画面"落后多少，拆成两块：
    #   1) 传输+解码：每帧交付时刻 - 该帧录制轴 PTS，实测，抖动取中位数；
    #      其中已含 RK3588 分发队列排队的时间（排得越久，交付越晚）。
    #   2) 冻结余量：画面连续静止时，最后的静止帧可能不是最新实况，
    #      由对时测得；平时为 0。
    # 对时（lat_probe）= 屏幕上真钟/发令时刻 - 当时画面里的录制轴时刻，
    # 得出总数再减去传输部分。比赛开场或换设备后按 `t` 一次即可。
    def _lat_show(self):
        """屏幕上这帧（估算）已是录制轴上第几秒

        兜底路径：没拿到 PTS 时按"录制起点到现在"外推。但录制早就停了、
        直播也没画面的时候，墙钟还在走，这个外推会一直涨（实测曾跑到
        172 小时），必须用已录长度封顶——否则界面上那个"本场时钟"看着
        像还在录制，实际相机根本没开。
        """
        now = time.time()
        base = self.archive.base_ms()
        if base is None:
            return 0.0
        est = max(0.0, now - base / 1000.0 - self._live_lat)
        if not self.feed.alive():
            # 没有直播帧：录制轴不可能超过已经写进盘的长度
            est = min(est, self.archive.last_end())
        return est

    def live_latency(self):
        """预估直播延迟（秒）；直播没画面时返回 None"""
        if not self.feed.alive():
            return None
        pts = self.feed.image_pts
        if pts is not None:
            return max(0.0, min(self._live_lat, 30.0))
        return None

    def transport_latency(self):
        return self.feed.transport_lat

    def _sample_latency(self):
        """主循环每帧调用：用帧 PTS 实测传输+解码延迟，并跟踪画面是否冻结"""
        if self.feed.image_t <= 0.0:
            return
        with self._lock:
            if self.feed.image_n != self._lat_frozen_n:
                self._lat_frozen_n = self.feed.image_n
                self._lat_frozen_t = time.monotonic()
        pts = self.feed.image_pts
        if pts is None:
            return
        dec_frozen = (self.feed.transport_lat is not None
                      and time.monotonic() - self._lat_frozen_t > 3.0)
        # 静帧时的"交付时刻-PTS"会无限增长，不能当延迟用
        trans = self.feed.transport_lat or 0.0
        if dec_frozen:
            trans = max(0.0, trans - (time.monotonic() - self._lat_frozen_t))
        with self._lock:
            self._live_lat = max(0.0, trans + self._lat_extra)

    def lat_probe(self, ref_wall=None):
        """对时：用刚才画面上那帧的录制轴时刻，对照一个已知的真实时刻。

        没有 ref_wall 时按"此刻拍到的画面"兜底（拿不到分发队列那一段，
        只会给出下限）。返回修正后的总延迟。
        """
        pts = self.feed.image_pts
        if pts is None:
            self._say('直播没带时间戳，无法对时')
            return None
        ref = time.time() if ref_wall is None else ref_wall
        total = ref - (self.archive.base_ms() / 1000.0 + pts)
        total = max(0.0, total)
        extra = total - (self.feed.transport_lat or 0.0)
        with self._lock:
            self._lat_extra = extra
            self._live_lat = total
        return total

    def lat_fix(self, delta_s):
        """人工微调延迟：觉得标记点已经偏了，用这个平移"""
        with self._lock:
            self._lat_extra = max(0.0, self._lat_extra + delta_s)
            self._live_lat = max(0.0, self._live_lat + delta_s)
        return self._live_lat

    # ---------- 比赛计时 ----------
    def reset_clock(self):
        self.clock_offset = 0.0
        self.clock_t0 = time.monotonic()
        self._say('计时归零')

    # ---------- 归档 ----------
    def _archive_cfg_path(self):
        return os.path.join(self.args.out_dir, 'archive.json')

    def _load_archive_dir(self):
        """读上次用的归档目录；没有就给个合理的默认值。"""
        try:
            with open(self._archive_cfg_path(), encoding='utf-8') as f:
                d = json.load(f)
            p = d.get('dir') if isinstance(d, dict) else None
            if isinstance(p, str) and p.strip():
                return os.path.expanduser(p.strip())
        except (OSError, ValueError):
            pass
        # 与 RK3588 版隔开：默认 ~/录像_rk，避免两份监看台的归档混在一起
        return os.path.join(os.path.expanduser('~'), '录像_rk')

    def set_archive_dir(self, path):
        """换归档目录：回放时 ClipLoader 要先在这里找已归档的段。"""
        self.archive_dir = os.path.expanduser(path.strip())
        d = self.archive_dir
        if d not in self.loader.local_dirs:
            self.loader.local_dirs.insert(0, d)

    def save_archive_dir(self, path):
        self.set_archive_dir(path)
        try:
            os.makedirs(self.args.out_dir, exist_ok=True)
            with open(self._archive_cfg_path(), 'w', encoding='utf-8') as f:
                json.dump({'dir': self.archive_dir}, f, ensure_ascii=False)
        except OSError as e:
            self._say('归档目录已设为 %s（保存失败: %s）' % (self.archive_dir, e))
            return
        self._say('归档目录：%s' % self.archive_dir)

    def session_name(self):
        """当前（或最近）场次目录名，用于停止后归档。"""
        try:
            name = os.path.basename(self.archive.session_name().rstrip('/'))
            if name:
                return name
        except Exception:
            pass
        return None

    def poll_archive_prompt(self):
        """UI 每帧调用。需要弹确认框时返回 (sess, info)，否则 None。

        查概况要 ssh 到 RK3588，不能放在主循环里同步做（会卡住画面），
        所以停止成功后只在后台线程里查，查完置 _archive_prompt。
        """
        sess = self._pending_archive_sess
        if sess and self._archive_probe is None:
            self._pending_archive_sess = None
            self._archive_probe = threading.Thread(
                target=self._probe_archive, args=(sess,), daemon=True)
            self._archive_probe.start()
            return None
        if self._archive_prompt:
            self._archive_prompt = False
            p = self._pending_archive
            # 场次为空或查不到概况就不打扰裁判
            if p and p[1] and p[1]['bytes'] > 0:
                return p
            self._pending_archive = None
        return None

    def _probe_archive(self, sess):
        try:
            info = self.archiver.info(sess)
            if info and info['bytes'] > 0:
                self._pending_archive = (sess, info)
                self._archive_prompt = True
            else:
                self._say('本场录像已在 RK3588 上，未找到可归档文件')
        except Exception as e:
            self._say('查询场次失败: %s: %s' % (type(e).__name__, e))
        finally:
            self._archive_probe = None

    def skip_archive(self):
        """裁判选择「稍后再归档」：清掉待归档状态。"""
        self._pending_archive = None
        self._archive_prompt = False

    def start_archive(self, dest_dir, remove_remote=False, sess=None,
                      total=0):
        """开始归档（后台线程）。dest_dir 为空则用记住的目录。"""
        if self.archive_busy:
            return False
        if not sess:
            if self._pending_archive is None:
                return False
            sess, _info = self._pending_archive
            total = _info['bytes']
        dest = (dest_dir or '').strip() or self.archive_dir
        self.save_archive_dir(dest)
        self.archive_busy = True
        self.archive_session = sess
        self.archive_done = 0
        self.archive_total = total
        self.archive_phase = 'connect'
        self._archive_prompt = False
        self.archive_cancel = threading.Event()
        threading.Thread(target=self._archive_worker,
                         args=(sess, dest, remove_remote),
                         daemon=True).start()
        return True

    def cancel_archive(self):
        if self.archive_cancel is not None:
            self.archive_cancel.set()
            self._say('正在取消归档…')

    def _archive_worker(self, sess, dest, remove_remote):
        def prog(done, total, phase):
            self.archive_done = done
            self.archive_total = total
            self.archive_phase = phase
        try:
            status, msg = self.archiver.archive(
                sess, dest, remove_remote=remove_remote,
                on_progress=prog, cancel_evt=self.archive_cancel)
            if status == 'OK':
                self._pending_archive = None
                self._say(msg)
            elif status == 'CANCELED':
                self._say(msg)
            else:
                self._say('归档失败: %s' % msg)
        except Exception as e:
            self._say('归档失败: %s: %s' % (type(e).__name__, e))
        finally:
            self.archive_busy = False
            self.archive_cancel = None

    def take_pending_archive(self):
        """UI 取走待确认归档信息（取一次即清）。"""
        p, self._pending_archive = self._pending_archive, None
        return p

    # ---------- 录制控制 ----------
    def _detect_recording(self):
        try:
            p = self.remote._ssh(
                "ps -eo comm=,args= | awk '$1==\"python3\" && "
                "/hk_pipeline_rk[.]py/ {f=1} END {exit !f}' && echo RUNNING "
                "|| echo STOPPED")
            if 'RUNNING' in p.stdout:
                self.recording = True
                self._say('检测到 RK3588 正在录制')
        except Exception:
            pass

    def start_recording(self):
        if self.remote_busy or self.recording:
            return
        self.remote_busy = True
        self.stop_confirm_t = 0.0
        self._say('正在准备相机并启动 RK3588 录制…')
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            status, out = self.remote.start()
            if status in ('ALREADY', 'STARTED'):
                self.recording = True
                self.feed.set_want(True)
                self._say('录制已启动' if status == 'STARTED'
                          else 'RK3588 已在录制')
            else:
                # 后端已复核进程存活，走到这里说明启动后立刻退出。
                # 最常见就是磁盘低于 --min-free-gb，原因已从远端日志带回来。
                self.recording = False
                self._say('录制启动失败（进程未存活）: %s'
                          % (out or '未知原因'))
        except Exception as e:
            self._say('启动失败: %s: %s' % (type(e).__name__, e))
        finally:
            self.remote_busy = False

    def stop_recording(self):
        if self.remote_busy:
            return
        now = time.monotonic()
        if now > self.stop_confirm_t:
            self.stop_confirm_t = now + 5.0
            self._say('5 秒内再点一次「停止」确认')
            return
        self.stop_confirm_t = 0.0
        self.remote_busy = True
        self._say('正在停止 RK3588 录制…')
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        try:
            # 必须在停之前抓场次名：Archive 轮询发现录制进程没了就不再
            # 更新 session，之后可能读到空串，归档就找不到目录了。
            sess_before = self.session_name()
            status, out = self.remote.stop()
            if status in ('STOPPED', 'NO_PROC'):
                self.recording = False
                self.feed.set_want(False)
                self._say('录制已停止' if status == 'STOPPED'
                          else 'RK3588 未在录制')
                # 把待归档场次登记好，UI 下一帧会弹确认框。
                # NO_PROC（本来就没在录）时场次名常为空，不能登记——空串会
                # 流到 Archiver.info('') → int('') → ValueError。
                sess = sess_before or self.session_name()
                if status == 'STOPPED' and sess:
                    self._pending_archive_sess = sess
                else:
                    self._pending_archive_sess = None
            elif status == 'TIMEOUT':
                # 进程收到 SIGTERM 但远端脚本等了 30 秒仍未退。
                # 别自作主张 SIGKILL（会丢当前分段、可能触发 USB 假死）。
                self._say('已发送停止信号，但进程 30 秒未退出，请核查')
            else:
                self._say('停止失败: %s' % out)
        except Exception as e:
            self._say('停止失败: %s: %s' % (type(e).__name__, e))
        finally:
            self.remote_busy = False

    # ---------- 小工具 ----------
    def _say(self, text):
        self.msg = text
        self.msg_t = time.monotonic()
        print(text, flush=True)


# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description='赛事监看台')
    # DGX 运行监看台，默认通过有线网络连接 RK3588。
    # 录制和原分辨率主码流编码保留在 RK3588，DGX 负责解码、显示和回放。
    ap.add_argument('--host', default='192.168.123.100')
    ap.add_argument('--live-port', type=int, default=9101)
    # 与 RK3588 版最大的差异：RK3588 上砍掉了缩略流——两路 mpph265enc 抢
    # MPP 硬件，主链路从 59fps 掉到 43.8fps。所以监看台只能接主码流：
    # thumb-port 默认 0，永远走 live-port。
    ap.add_argument('--thumb-port', type=int, default=0,
                    help='缩略直播端口（0=使用主码流，RK3588 默认）')
    ap.add_argument('--remote', default='rk3588',
                    help='RK3588 SSH 别名（默认 rk3588）')
    ap.add_argument('--remote-root', default='~/hk_record',
                    help='录制输出根目录（本机模式即管线 --out）')
    ap.add_argument('--cache-dir',
                    default=os.path.join(os.path.dirname(__file__), 'replay', 'cache_rk'))
    ap.add_argument('--cache-max-gb', type=float, default=20.0)
    ap.add_argument('--out-dir', default=os.path.join(os.path.dirname(__file__), 'replay', 'rk'))
    ap.add_argument('--display-width', type=int, default=1280)
    ap.add_argument('--window-width', type=int, default=1600)
    ap.add_argument('--window-height', type=int, default=1000)
    ap.add_argument('--speed', type=int, default=0, choices=(0, 1, 2))
    ap.add_argument('--archive-interval', type=float, default=3.0)
    ap.add_argument('--rec-segment', type=float, default=10.0)
    ap.add_argument('--rec-ring', type=float, default=90.0)
    ap.add_argument('--rec-min-free-gb', type=float, default=10.0)
    # 裁判数据服务：平板/手机读时间码、场次、标记，也能远程打标记。
    # 0 表示关闭。RK3588 版占 8610、referee_monitor 占 8600，
    # RK3588 版用 8611，两个监看台同时开也不抢端口。
    ap.add_argument('--referee-port', type=int, default=8611,
                    help='裁判数据 HTTP 端口（0=关闭，RK3588 默认 8611）')
    return ap.parse_args()


def main():
    args = parse_args()
    if not os.environ.get('DISPLAY'):
        print('没有 DISPLAY，无法开窗口')
        return 2

    import tkinter as tk
    from studio_ui_rk import StudioUI

    root = tk.Tk()
    ctl = StudioController(args)
    ui = StudioUI(root, ctl, args)
    ctl.start()

    # 平板端读状态、打标记、遥控回放都走这个 HTTP 服务（同进程，
    # 读的是实时对象；写操作由 pad.pump() 转回 Tk 主线程执行）
    referee = None
    if args.referee_port > 0:
        from referee_data_rk import PadServer
        referee = PadServer(ctl, port=args.referee_port)
        referee.start()

    # 主循环：~30fps 刷新，同时驱动回放推进
    def loop():
        if ctl.quit_requested():
            ctl.shutdown()
            root.destroy()
            return
        try:
            delay = ctl.tick()
            if referee is not None:
                referee.pump()
            ui.refresh()
        except Exception as e:
            print('loop error: %s: %s' % (type(e).__name__, e), flush=True)
            delay = None
        interval = 33
        if delay is not None:
            interval = max(1, min(33, int(delay * 1000)))
        root.after(interval, loop)

    root.after(30, loop)
    try:
        root.mainloop()
    finally:
        if referee is not None:
            referee.stop()
        ctl.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
