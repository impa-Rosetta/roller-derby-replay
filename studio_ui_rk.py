#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛事监看台 UI 层：tkinter 深色专业界面

布局：
  ┌──────────────────────────────────────────┐
  │ 顶部状态栏：●LIVE 帧率 时钟 录制状态 计时  │
  ├───────────────────────────┬──────────────┤
  │                           │ 直播小窗      │
  │                           │──────────────│
  │        主画面 Canvas       │ 片段列表      │
  │                           │──────────────│
  │                           │ 标记列表      │
  │                           │──────────────│
  │                           │ 状态详情      │
  ├───────────────────────────┴──────────────┤
  │ 底部控制栏：播放 速度 逐帧 标记 时间轴     │
  └──────────────────────────────────────────┘

只依赖 tkinter + 服务层（studio_services），不直接碰网络。
"""
import json
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, font as tkfont

import cv2
import numpy as np

import studio_services_rk as svc
import studio_theme as T
from studio_services_rk import fmt_time, fmt_size

# Tk 8.6 在 HiDPI 下按 points 放大字体（本例 110dpi → 1.53x），但 Canvas
# 像素尺寸 / Listbox 行高不跟，导致 UI 元素与文字不成比例。这里统一换算。
_DPI_SCALE = 1.0


def px(v):
    """设计像素（96dpi 基准）→ 当前显示器像素"""
    return int(round(v * _DPI_SCALE))


# ---------------------------------------------------------------------------
# 自定义圆角按钮（Canvas 实现）
# ---------------------------------------------------------------------------

class FlatButton(tk.Canvas):
    """Canvas 圆角按钮：支持主色 / 危险 / 普通、悬停、按下、禁用态"""

    def __init__(self, parent, text, command=None, width=120, height=40,
                 style='normal', font=T.F_BTN, radius=8):
        w, h = px(width), px(height)
        super().__init__(parent, width=w, height=h,
                         highlightthickness=0, bd=0,
                         bg=parent['bg'] if 'bg' in parent.keys() else T.BG_PANEL)
        self._text = text
        self._cmd = command
        self._bw = w
        self._bh = h
        self._style = style
        self._font = font
        self._r = px(radius)
        self._enabled = True
        self._hover = False
        self._pressed = False
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<ButtonPress-1>', self._on_press)
        self.bind('<ButtonRelease-1>', self._on_release)
        self._redraw()

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._redraw()

    def set_style(self, style):
        if style != self._style:
            self._style = style
            self._redraw()

    def set_enabled(self, enabled):
        if enabled != self._enabled:
            self._enabled = enabled
            self._hover = self._pressed = False
            self._redraw()

    def set_active(self, active):
        """视觉高亮（如当前模式）"""
        self._active = getattr(self, '_active', False)
        if active != self._active:
            self._active = active
            self._redraw()

    def _palette(self):
        if not self._enabled:
            return T.BG_RAISED, T.FG_DIM, T.BORDER
        active = getattr(self, '_active', False)
        if self._style == 'primary':
            base, fg = T.INFO_BLUE_D, '#ffffff'
            if self._pressed:
                base = '#1158c7'
            elif self._hover:
                base = '#2b7fe0'
        elif self._style == 'danger':
            base, fg = T.REC_RED_D, '#ffffff'
            if self._pressed:
                base = '#a42a22'
            elif self._hover:
                base = '#e0493f'
        elif self._style == 'live':
            base, fg = T.LIVE_GREEN_D, '#ffffff'
            if self._pressed:
                base = '#1a6e2a'
            elif self._hover:
                base = '#2ea043'
        elif self._style == 'mark':
            base, fg = '#6e40a0', '#ffffff'
            if self._pressed:
                base = '#58328a'
            elif self._hover:
                base = '#7f4ecc'
        else:  # normal
            if active:
                base, fg = T.INFO_BLUE_D, '#ffffff'
            else:
                base, fg = T.BG_RAISED, T.FG_TEXT
                if self._pressed:
                    base = T.BG_ACTIVE
                elif self._hover:
                    base = T.BG_HOVER
        return base, fg, T.BORDER_LT

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
               x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
               x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _redraw(self):
        self.delete('all')
        base, fg, outline = self._palette()
        r = self._r
        self._round_rect(1, 1, self._bw - 1, self._bh - 1, r,
                         fill=base, outline=outline)
        self.create_text(self._bw // 2, self._bh // 2, text=self._text,
                         fill=fg, font=self._font)

    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _e):
        if self._enabled:
            self._hover = self._pressed = False
            self._redraw()

    def _on_press(self, _e):
        if self._enabled:
            self._pressed = True
            self._redraw()

    def _on_release(self, _e):
        was = self._pressed
        self._pressed = False
        self._redraw()
        if was and self._enabled and self._cmd is not None:
            self._cmd()


# ---------------------------------------------------------------------------
# 顶栏状态徽章
# ---------------------------------------------------------------------------

class StatusBadge(tk.Frame):
    """带圆点的状态徽章：● LABEL"""

    def __init__(self, parent, text, color=T.FG_MUTED, font=T.F_BADGE,
                 dot=True):
        super().__init__(parent, bg=parent['bg'])
        d = px(14)
        self._dot = tk.Canvas(self, width=d, height=d,
                              highlightthickness=0, bd=0,
                              bg=parent['bg'])
        m = d * 0.3
        self._dot_id = self._dot.create_oval(m, m, d - m, d - m, fill=color,
                                             outline='')
        self._dot.pack(side='left')
        self._label = tk.Label(self, text=text, fg=T.FG_TEXT,
                               bg=parent['bg'], font=font, anchor='w')
        self._label.pack(side='left', padx=(2, 0))

    def set(self, text=None, color=None):
        if text is not None:
            self._label.config(text=text)
        if color is not None:
            self._dot.itemconfig(self._dot_id, fill=color)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class StudioUI:
    def __init__(self, root, controller, args, embedded=False):
        global _DPI_SCALE
        _DPI_SCALE = max(1.0, min(1.25, float(
            root.tk.call('tk', 'scaling')) / 1.3333))
        self.root = root
        self.ctl = controller      # StudioController：提供状态与动作
        self.args = args
        self.embedded = embedded
        self._photo = None
        self._img_id = None
        self._placeholder_id = None
        self._live_photo = None
        self._last_render_t = 0.0
        self._last_frame_key = None
        self._seg_sig = None
        self._seg_cur = -1
        self._mark_sig = None
        self._flash_t = 0.0
        self._tl_hover = None
        self._dialog_open = False   # 模态框开着时别再弹

        root.configure(bg=T.BG_APP)
        if not embedded:
            root.title('RK3588 赛事监看台 · %s' % args.host)
            root.geometry('%dx%d' % (args.window_width, args.window_height))
            root.minsize(1100, 720)

        self._build_topbar()
        self._build_body()
        self._build_controlbar()

        if not embedded:
            root.protocol('WM_DELETE_WINDOW', self.ctl.request_quit)
            self._bind_keys()

    def _bind_keys(self):
        """键盘快捷键：裁判现场单手操作"""
        r = self.root
        r.bind('<space>', lambda e: self._key(self.ctl.toggle_play))
        r.bind('<Left>', lambda e: self._key(lambda: self.ctl.step(-1)))
        r.bind('<Right>', lambda e: self._key(lambda: self.ctl.step(1)))
        r.bind('<Shift-Left>', lambda e: self._key(lambda: self.ctl.step(-10)))
        r.bind('<Shift-Right>', lambda e: self._key(lambda: self.ctl.step(10)))
        r.bind('<Control-Left>', lambda e: self._key(
            lambda: self._nudge(-5.0)))
        r.bind('<Control-Right>', lambda e: self._key(
            lambda: self._nudge(5.0)))
        r.bind('n', lambda e: self._key(self._whistle_note))
        r.bind('m', lambda e: self._key(lambda: self._whistle(3.0)))
        r.bind('b', lambda e: self._key(lambda: self._whistle(0.0)))
        r.bind('<Control-e>', lambda e: self._key(self.ctl.export_marks))
        # 哨声对时：Ctrl+S 把标记时屏幕上那帧当作"哨响"，补上没测到的迟滞
        r.bind('<Control-s>', lambda e: self._key(self._on_whistle_lat))
        r.bind('<Control-d>', lambda e: self._key(lambda: self._mark_fix(1.0)))
        r.bind('<Control-f>', lambda e: self._key(lambda: self._mark_fix(-1.0)))
        r.bind('<F1>', lambda e: self._key(self.ctl.to_live))
        r.bind('<F2>', lambda e: self._key(self.ctl.to_replay))
        r.bind('<F5>', lambda e: self._key(self.ctl.snapshot))
        # F9 走和按钮同一条路径：先选归档目录，再起 RK3588
        r.bind('<F9>', lambda e: self._key(self._begin_recording))
        r.bind('<F10>', lambda e: self._key(self.ctl.stop_recording))
        r.bind('1', lambda e: self._key(lambda: self.ctl.set_speed(0)))
        r.bind('2', lambda e: self._key(lambda: self.ctl.set_speed(1)))
        r.bind('3', lambda e: self._key(lambda: self.ctl.set_speed(2)))
        r.bind('<Control-r>', lambda e: self._key(self.ctl.reset_clock))
        r.bind('<Home>', lambda e: self._key(lambda: self.ctl.seek(0.0)))

    def _key(self, fn):
        """键盘回调：列表有焦点时不要吞掉输入法/文本键"""
        focus = self.root.focus_get()
        if isinstance(focus, tk.Listbox):
            return 'break'
        try:
            fn()
        except Exception as e:
            print('key error: %s: %s' % (type(e).__name__, e), flush=True)
        return 'break'

    def _nudge(self, sec):
        self.ctl.seek(max(0.0, self.ctl.position_s() + sec))

    def _whistle(self, pre_s):
        """吹哨标记：直播下按哨声标真实时刻（扣除延迟），回放下标当前点"""
        est = self.ctl.mode == 'live'
        m = self.ctl.add_mark(pre_s=pre_s, est=est)
        if m is not None:
            self._flash('⚑ %s  %s%s' % (
                m.get('wall_local', ''), m.get('clock', ''),
                '  (直播估算)' if m.get('wall_est') else ''))

    def _on_whistle_lat(self):
        """哨声已响、画面才刚到：把这帧当作哨响时刻，校准延迟"""
        lat = self.ctl.note_broken()
        if lat is not None:
            self._flash('延迟已校准：直播 %.1fs' % lat)

    def _mark_fix(self, sec):
        """所有标记整体平移 ±1 秒（Ctrl+D 推后、Ctrl+F 提前）"""
        lead = self.ctl.fix_marks(sec)
        self._flash('标记偏移 %+.0fs（当前 %+.1fs）' % (sec, lead))

    def _add_note(self):
        """给最近一条标记补备注（无标记时提示）"""
        if not self.ctl.marks():
            self._flash('还没有标记，先按 N 或点「吹哨」')
            return
        from tkinter import simpledialog
        txt = simpledialog.askstring('标记备注', '备注内容：', parent=self.root)
        if txt:
            self.ctl.mark_note(txt)

    def _delete_mark(self):
        """删掉选中的标记（列表是倒序显示，索引要翻回来）。"""
        if not self.ctl.marks():
            self._flash('还没有标记')
            return
        sel = self.mark_list.curselection()
        if not sel:
            self._flash('先在标记列表里点一条')
            return
        i = len(self.ctl.marks()) - 1 - sel[0]
        note = self.ctl.marks()[i].get('note', '')
        from tkinter import messagebox
        if not messagebox.askyesno('删除标记', '删掉这条标记？\n\n%s'
                                   % note, parent=self.root):
            return
        if self.ctl.remove_mark(i):
            self._flash('已删除标记 %s' % note)

    def _update_history_ui(self, st):
        """打开/关闭历史录像时切换顶栏徽标、徽标后面那行来源目录。"""
        hist = bool(st.get('history'))
        if hist != self._hist_state:
            self._hist_state = hist
            if hist:
                self.badge_hist.pack(side='left', padx=(px(6), 0))
                self.lbl_src.pack(side='left', padx=(px(6), 0))
                self.badge_mode.set(text='录像', color=T.MARK_PURPLE)
            else:
                self.badge_hist.pack_forget()
                self.lbl_src.pack_forget()
                self.badge_mode.set(text='直播', color=T.INFO_BLUE)
        if hist:
            p = st.get('history_root') or ''
            # 只显示末两级，整条路径太长顶栏放不下
            parts = [x for x in p.split(os.sep) if x]
            short = os.sep.join(parts[-2:]) if len(parts) > 2 else p
            self.lbl_src.config(text='来源 %s' % short)
        else:
            self.lbl_src.config(text='')

    def _on_library(self):
        """「录像库」按钮：列出本机所有场次，可回看/删除。

        非模态窗口，直播不会被挡住；打开某场后窗口自己关掉，
        主画面按「历史场次」模式回放该场（标记随之载入）。
        """
        if getattr(self, '_lib_win', None) is not None:
            try:
                self._lib_win.lift()
                self._lib_win.focus_force()
                return
            except tk.TclError:
                self._lib_win = None
        self._lib_win = LibraryDialog(self.root, self.ctl,
                                      self._on_library_open)

    def _on_library_open(self, name):
        self._lib_win = None
        self._flash('已从录像库打开 %s' % name)

    def _on_open_session(self):
        """「打开录像」按钮：弹场次选择框。"""
        if self._dialog_open:
            return
        dlg = OpenSessionDialog(
            self.root, self.ctl,
            self.ctl.history_root() or self.ctl.archive_dir)
        if dlg.result:
            name = self.ctl.open_session(dlg.result)
            if name:
                self._flash('已打开历史场次 %s' % name)

    def _flash(self, text):
        self._flash_t = time.monotonic()
        self.lbl_status.config(text=text)
        # 顶栏主行下方临时提示（状态栏在窗口底部，裁判看画面时看不见）
        self.lbl_toast.config(text=text)

    def _clear_toast(self):
        if time.monotonic() - self._flash_t > 4.0:
            self.lbl_toast.config(text='')

    def _whistle_note(self):
        """N 键：直播下"吹哨 + 立即弹备注框"一步到位"""
        self._whistle(3.0)
        self._add_note()

    # ---------- 顶栏 ----------
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=T.BG_PANEL, height=px(56))
        bar.pack(side='top', fill='x')
        bar.pack_propagate(False)
        tk.Frame(self.root, bg=T.BORDER, height=1).pack(side='top', fill='x')

        left = tk.Frame(bar, bg=T.BG_PANEL)
        left.pack(side='left', padx=px(14))
        tk.Label(left, text='赛事监看台', fg=T.FG_TEXT, bg=T.BG_PANEL,
                 font=T.F_TITLE).pack(side='left')
        self.badge_live = StatusBadge(left, 'LIVE', T.FG_MUTED)
        self.badge_live.pack(side='left', padx=(16, 6))
        self.badge_fps = StatusBadge(left, '-- fps', T.FG_MUTED, dot=False)
        self.badge_fps.pack(side='left', padx=(0, 6))
        self.badge_rec = StatusBadge(left, '未录制', T.FG_MUTED)
        self.badge_rec.pack(side='left', padx=(0, 6))
        self.badge_mode = StatusBadge(left, '直播', T.INFO_BLUE, dot=False)
        self.badge_mode.pack(side='left')
        self.badge_hist = StatusBadge(left, '历史场次', T.MARK_PURPLE,
                                      dot=False)
        self.lbl_src = tk.Label(left, text='', fg=T.FG_DIM, bg=T.BG_PANEL,
                                font=T.F_TINY)
        self._hist_state = False
        # 默认不显示，打开历史录像时才 pack 出来（见 _update_history_ui）

        right = tk.Frame(bar, bg=T.BG_PANEL)
        right.pack(side='right', padx=px(14))
        # 主时间显示：绝对时钟（与裁判手表、录像文件对齐）
        self.lbl_wall = tk.Label(right, text='--:--:--', fg=T.FG_TEXT,
                                 bg=T.BG_PANEL, font=T.F_CLOCK)
        self.lbl_wall.pack(side='right')
        # 副行：本场时钟 + 绝对帧号
        sub = tk.Frame(right, bg=T.BG_PANEL)
        sub.pack(side='right', padx=(0, px(12)))
        self.lbl_tc = tk.Label(sub, text='00:00:00  #0', fg=T.FG_MUTED,
                               bg=T.BG_PANEL, font=T.F_HUD, anchor='e')
        self.lbl_tc.pack()
        self.lbl_clock = tk.Label(sub, text='本场 00:00:00', fg=T.FG_DIM,
                                  bg=T.BG_PANEL, font=T.F_SMALL, anchor='e')
        self.lbl_clock.pack()
        self.lbl_toast = tk.Label(right, text='', fg=T.WARN_AMBER,
                                  bg=T.BG_PANEL, font=T.F_SMALL)
        self.lbl_toast.pack(side='right', padx=(px(16), 0))

        # 本场标识（当前录制会话 / 本场时长）
        mid = tk.Frame(bar, bg=T.BG_PANEL)
        mid.pack(side='right', padx=(0, px(18)))
        self.lbl_session = tk.Label(
            mid, text='本场 —', fg=T.FG_MUTED, bg=T.BG_PANEL,
            font=T.F_SMALL, anchor='e')
        self.lbl_session.pack(side='top', pady=(px(9), 0))
        self.lbl_session_len = tk.Label(
            mid, text='时长 00:00', fg=T.FG_DIM, bg=T.BG_PANEL,
            font=T.F_TINY, anchor='e')
        self.lbl_session_len.pack(side='top')

    # ---------- 主体 ----------
    def _build_body(self):
        body = tk.Frame(self.root, bg=T.BG_APP)
        body.pack(side='top', fill='both', expand=True)

        # 左：主画面
        left = tk.Frame(body, bg=T.BG_APP)
        left.pack(side='left', fill='both', expand=True, padx=(px(10), px(6)),
                  pady=px(8))
        self.video_wrap = tk.Frame(left, bg='#000000')
        self.video_wrap.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(self.video_wrap, bg='#000000',
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', self._on_canvas_resize)
        self._cw = self._ch = 0

        # 右：信息面板
        right = tk.Frame(body, bg=T.BG_PANEL, width=px(360))
        right.pack(side='right', fill='y', padx=(px(6), px(10)), pady=px(8))
        right.pack_propagate(False)

        # 直播小窗
        self._section(right, '直播小窗')
        self.live_canvas = tk.Canvas(right, height=px(150), bg='#050708',
                                     highlightthickness=1,
                                     highlightbackground=T.BORDER)
        self.live_canvas.pack(fill='x', padx=px(10), pady=(0, px(4)))

        # 列表区（片段/标记按权重分配，可各自滚动）
        lists = tk.Frame(right, bg=T.BG_PANEL)
        lists.pack(fill='both', expand=True)
        lists.grid_rowconfigure(0, weight=4)   # 片段 4 : 标记 3
        lists.grid_rowconfigure(1, weight=3)
        lists.grid_columnconfigure(0, weight=1)

        # 片段列表
        seg_box = tk.Frame(lists, bg=T.BG_PANEL)
        seg_box.grid(row=0, column=0, sticky='nsew')
        self._section(seg_box, '片段列表')
        seg_frame = tk.Frame(seg_box, bg=T.BG_PANEL)
        seg_frame.pack(fill='both', expand=True, padx=px(10), pady=(0, px(6)))
        self.seg_list = tk.Listbox(
            seg_frame, bg=T.BG_RAISED, fg=T.FG_TEXT, font=T.F_SMALL,
            selectbackground=T.INFO_BLUE_D, selectforeground='#ffffff',
            highlightthickness=0, bd=0, activestyle='none', exportselection=False,
            selectborderwidth=0)
        sb = tk.Scrollbar(seg_frame, orient='vertical',
                          command=self.seg_list.yview,
                          width=px(10), troughcolor=T.BG_RAISED,
                          bg=T.BG_ACTIVE, activebackground=T.BORDER_LT,
                          relief='flat', bd=0, highlightthickness=0)
        self.seg_list.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.seg_list.pack(side='left', fill='both', expand=True)
        self.seg_list.bind('<<ListboxSelect>>', self._on_seg_select)

        # 标记列表
        mark_box = tk.Frame(lists, bg=T.BG_PANEL)
        mark_box.grid(row=1, column=0, sticky='nsew')
        self._section(mark_box, '判罚标记')
        mark_frame = tk.Frame(mark_box, bg=T.BG_PANEL)
        mark_frame.pack(fill='both', expand=True, padx=px(10), pady=(0, px(6)))
        self.mark_list = tk.Listbox(
            mark_frame, bg=T.BG_RAISED, fg=T.MARK_PURPLE, font=T.F_SMALL,
            selectbackground='#6e40a0', selectforeground='#ffffff',
            highlightthickness=0, bd=0, activestyle='none', exportselection=False,
            selectborderwidth=0)
        msb = tk.Scrollbar(mark_frame, orient='vertical',
                           command=self.mark_list.yview,
                           width=px(10), troughcolor=T.BG_RAISED,
                           bg=T.BG_ACTIVE, activebackground=T.BORDER_LT,
                           relief='flat', bd=0, highlightthickness=0)
        self.mark_list.config(yscrollcommand=msb.set)
        msb.pack(side='right', fill='y')
        self.mark_list.pack(side='left', fill='both', expand=True)
        self.mark_list.bind('<<ListboxSelect>>', self._on_mark_select)
        # Delete 键删标记只能在列表有焦点时触发，不会误伤画面上的快捷键
        self.mark_list.bind('<Delete>', lambda e: self._delete_mark())
        self.mark_list.bind('<BackSpace>', lambda e: self._delete_mark())

        # 状态详情
        self._section(right, '状态')
        self.lbl_status = tk.Label(
            right, text='', fg=T.FG_MUTED, bg=T.BG_PANEL, font=T.F_TINY,
            justify='left', anchor='nw', wraplength=px(330))
        self.lbl_status.pack(fill='x', padx=px(12), pady=(0, px(4)))

        self._section(right, '快捷键')
        keys = tk.Label(
            right,
            text=('N 吹哨标记(-3s)+备注   M 吹哨标记   B 原地标记\n'
                  'Ctrl+S 延迟校准   Ctrl+D/F 标记±1s\n'
                  '空格 播放/暂停   ← → 逐帧   Shift+←→ 10帧\n'
                  'Ctrl+←→ ±5秒   1/2/3 变速   Ctrl+E 导出标记\n'
                  'F1 直播  F2 回放  F5 截图  F9/F10 开始/停止录制'),
            fg=T.FG_DIM, bg=T.BG_PANEL, font=T.F_TINY,
            justify='left', anchor='nw', wraplength=px(330))
        keys.pack(fill='x', padx=px(12), pady=(0, px(8)))

    def _section(self, parent, title):
        f = tk.Frame(parent, bg=T.BG_PANEL)
        f.pack(fill='x', padx=px(12), pady=(px(8), px(2)))
        tk.Label(f, text=title, fg=T.FG_MUTED, bg=T.BG_PANEL,
                 font=T.F_SMALL).pack(side='left')

    # ---------- 底栏 ----------
    def _build_controlbar(self):
        tk.Frame(self.root, bg=T.BORDER, height=1).pack(side='top', fill='x')
        bar = tk.Frame(self.root, bg=T.BG_PANEL,
                       height=px(150 if self.embedded else 104))
        bar.pack(side='bottom', fill='x')
        bar.pack_propagate(False)

        # 第一行：按钮
        row = tk.Frame(bar, bg=T.BG_PANEL)
        row.pack(fill='x', padx=px(12), pady=(px(10), px(4)))

        def mkbtn(text, cmd, style='normal', w=76, font=T.F_BTN):
            b = FlatButton(row, text, command=cmd, width=w, height=38,
                           style=style, font=font)
            b.pack(side='left', padx=(0, px(6)))
            return b

        # 开始录制前先选归档目录（录制期间仍写 RK3588，停止后再拷回来）
        self.btn_start = mkbtn('● 开始录制', self._begin_recording,
                               'live', 108)
        self.btn_stop = mkbtn('■ 停止', self.ctl.stop_recording, 'danger', 76)
        self.btn_live = mkbtn('直播', self.ctl.to_live, 'normal', 70)
        self.btn_replay = mkbtn('回放', self.ctl.to_replay, 'normal', 70)
        self.btn_library = mkbtn('录像库', self._on_library, 'normal', 80)
        self.btn_open = mkbtn('打开录像', self._on_open_session,
                              'normal', 92)
        self.btn_del = mkbtn('删标记', self._delete_mark, 'normal', 82)
        self.btn_close = mkbtn('关闭录像',
                               lambda: self.ctl.close_session(),
                               'normal', 92)
        if self.embedded:
            # The unified window has a navigation bar; keep every playback
            # control visible even on a 1200-pixel-wide desktop.
            row = tk.Frame(bar, bg=T.BG_PANEL)
            row.pack(fill='x', padx=px(12), pady=(0, px(4)))
        self.btn_play = mkbtn('▶ 播放', self.ctl.toggle_play, 'normal', 88)
        self.btn_back = mkbtn('◀ 帧', lambda: self.ctl.step(-1), 'normal', 66)
        self.btn_fwd = mkbtn('帧 ▶', lambda: self.ctl.step(1), 'normal', 66)

        tk.Frame(row, bg=T.BORDER_LT, width=1, height=px(30)).pack(
            side='left', padx=px(6))
        self.btn_s1 = mkbtn('1x', lambda: self.ctl.set_speed(0), 'normal', 50)
        self.btn_s2 = mkbtn('0.5x', lambda: self.ctl.set_speed(1), 'normal', 58)
        self.btn_s3 = mkbtn('0.25x', lambda: self.ctl.set_speed(2),
                            'normal', 66)
        self.btn_whistle = mkbtn(
            '⚑ 吹哨 -3s', lambda: self._whistle(3.0), 'mark', 108)
        self.btn_mark = mkbtn('标记', lambda: self._whistle(0.0), 'mark', 66)
        self.btn_note = mkbtn('备注', self._add_note, 'normal', 66)
        self.btn_export = mkbtn('导出标记', self.ctl.export_marks,
                                'normal', 92)
        self.btn_shot = mkbtn('截图', self.ctl.snapshot, 'normal', 66)
        self.btn_quit = mkbtn('退出', self.ctl.request_quit, 'danger', 66)

        # 第二行：时间轴
        tl = tk.Frame(bar, bg=T.BG_PANEL)
        tl.pack(fill='x', padx=px(12), pady=(px(2), px(8)))
        self.lbl_pos = tk.Label(tl, text='00:00', fg=T.FG_TEXT,
                                bg=T.BG_PANEL, font=T.F_HUD, width=7,
                                anchor='e')
        self.lbl_pos.pack(side='left')
        self.timeline = tk.Canvas(tl, height=px(38), bg=T.BG_RAISED,
                                  highlightthickness=1,
                                  highlightbackground=T.BORDER)
        self.timeline.pack(side='left', fill='x', expand=True, padx=px(10))
        self.timeline.bind('<Button-1>', self._on_timeline_click)
        self.timeline.bind('<B1-Motion>', self._on_timeline_drag)
        self.timeline.bind('<ButtonRelease-1>', self._on_timeline_release)
        self.timeline.bind('<Configure>', lambda e: self._draw_timeline())
        # 悬停时间提示（canvas 上的自绘小气泡）
        self.timeline.bind('<Motion>', self._on_timeline_motion)
        self.timeline.bind('<Leave>', self._on_timeline_leave)
        self._tl_dragging = False
        self._tl_hover = None       # 悬停锚点（录制轴秒）；None = 不显示
        self._tl_tip = {}           # 只是画在时间轴上的 cell，不碰 _draw_timeline
        self.lbl_dur = tk.Label(tl, text='00:00', fg=T.FG_MUTED,
                                bg=T.BG_PANEL, font=T.F_HUD, width=7,
                                anchor='w')
        self.lbl_dur.pack(side='left')

    # ---------- 视频渲染 ----------
    def _on_canvas_resize(self, e):
        self._cw, self._ch = e.width, e.height
        if self._cw > 0 and self._ch > 0:
            self._img_id = None   # 强制重贴
            self._placeholder_id = None
            self._render_main(force=True)

    def _render_main(self, force=False):
        img = self.ctl.current_frame()
        if img is None:
            # 必须真的清掉上一帧：_img_id 非 None 说明画布上还挂着旧图
            # （从直播切回放时尤其明显，会一直显示直播残影）。
            if force or self._img_id is not None or self._placeholder_id is None:
                self.canvas.delete('all')
                self._img_id = None
                self._last_frame_key = None
                self._placeholder_id = self.canvas.create_text(
                    self._cw // 2 if self._cw else 300,
                    self._ch // 2 if self._ch else 200,
                    text=self.ctl.placeholder_text(), fill=T.FG_MUTED,
                    font=(T.FONT_FAMILY, 16))
            else:
                self.canvas.itemconfigure(self._placeholder_id,
                                          text=self.ctl.placeholder_text())
            return
        if self._cw <= 0 or self._ch <= 0:
            return
        # 等比缩放铺满（letterbox）
        h, w = img.shape[:2]
        scale = min(self._cw / w, self._ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        # 缓存键必须能区分"同一帧"与"新帧"。
        # 注意：不能用 (img is last_frame_obj)——current_frame() 刚把
        # last_frame_obj 赋成 img，该布尔值恒为 True，画面会永远停在第一帧。
        # 用 id(img) 识别帧对象，配合模式/尺寸，换帧才重绘。
        key = (id(img), self._cw, self._ch, self.ctl.mode)
        if not force and self._last_frame_key == key and self._img_id:
            return
        frame = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        hdr = b'P6 %d %d 255\n' % (nw, nh)
        ppm = hdr + frame.tobytes()
        if self._photo is None:
            self._photo = tk.PhotoImage(master=self.canvas, width=nw,
                                        height=nh)
        else:
            self._photo.configure(width=nw, height=nh)
        self._photo.put(ppm, to=(0, 0))
        ox = (self._cw - nw) // 2
        oy = (self._ch - nh) // 2
        self.canvas.delete('all')
        self._placeholder_id = None   # 有画面了，占位文本已被 delete('all') 清掉
        self._img_id = self.canvas.create_image(ox, oy, anchor='nw',
                                                image=self._photo)
        self._last_frame_key = key
        # 直播水印
        if self.ctl.mode == 'live':
            self._draw_live_hud(ox, oy, nw, nh)

    def _draw_live_hud(self, ox, oy, nw, nh):
        fps = self.ctl.live_fps()
        self.canvas.create_rectangle(ox + 12, oy + 12, ox + 130, oy + 44,
                                     fill='#000000', outline='',
                                     stipple='gray50')
        self.canvas.create_oval(ox + 22, oy + 24, ox + 32, oy + 34,
                                fill=T.REC_RED, outline='')
        self.canvas.create_text(ox + 40, oy + 28, anchor='w', text='LIVE',
                                fill='#ffffff', font=T.F_HUD)
        self.canvas.create_text(ox + nw - 14, oy + 28, anchor='e',
                                text='%.1f fps' % fps, fill='#ffffff',
                                font=T.F_HUD)

    def _render_live_pip(self):
        img = self.ctl.live_frame()
        c = self.live_canvas
        if img is None:
            c.delete('all')
            return
        try:
            cw = c.winfo_width()
            chh = c.winfo_height()
        except Exception:
            return
        if cw <= 1 or chh <= 1:
            return
        h, w = img.shape[:2]
        scale = min(cw / w, chh / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        frame = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if self._live_photo is None:
            self._live_photo = tk.PhotoImage(master=c, width=nw, height=nh)
        self._live_photo.put(b'P6 %d %d 255\n' % (nw, nh) + frame.tobytes(),
                             to=(0, 0))
        c.delete('all')
        c.create_image((cw - nw) // 2, (chh - nh) // 2, anchor='nw',
                       image=self._live_photo)

    # ---------- 时间轴 ----------
    def _tl_geometry(self):
        w = self.timeline.winfo_width()
        h = self.timeline.winfo_height()
        return w, h

    def _draw_timeline(self):
        c = self.timeline
        w, h = self._tl_geometry()
        if w <= 1:
            return
        c.delete('all')
        segs, t0 = self._tl_window()
        total = max(1.0, self.ctl.timeline_end())
        t1 = max(t0 + 1.0, total)
        span = t1 - t0

        def tx(t):
            return (t - t0) / span * w

        # 分段色块（偶数段交替）
        base = self._tl_seg_origin()
        for i, s in enumerate(segs):
            x0 = int(tx(s['start_s']))
            x1 = int(tx(s['end_s']))
            col = T.BG_ACTIVE if (base + i) % 2 else T.BG_HOVER
            c.create_rectangle(x0, 2, max(x0 + 1, x1), h - 2, fill=col,
                               outline='')
        # 标记
        for m in self.ctl.marks():
            if t0 <= m['t_s'] <= t1:
                mx = int(tx(m['t_s']))
                c.create_polygon(mx, 0, mx + 7, 0, mx, 9,
                                 fill=T.MARK_PURPLE, outline='')
        # 播放头
        pos = self.ctl.position_s()
        px = int(max(0, min(w, tx(pos))))
        c.create_line(px, 0, px, h, fill=T.INFO_BLUE, width=2)
        # 刻标（时间轴右端已有独立的时长标签，这里只标起点）
        lbl = fmt_time(t0) if t0 > 0 else ''
        if lbl:
            c.create_text(6, h - 6, anchor='sw', text=lbl,
                          fill=T.FG_DIM, font=T.F_TINY)
        # 重绘会清掉悬停提示，按上次的锚点补回来（否则每帧都会闪掉）
        self._tl_tip = {}
        if self._tl_hover is not None:
            t = max(t0, min(t1, self._tl_hover))
            self._draw_hover_tip(int(max(0, min(w, tx(t)))))

    def _tl_window(self, span_s=None):
        """默认从0秒显示完整场次，随总时长自动缩放。"""
        segs, t0 = self.ctl.segments_window(span_s)
        return segs, t0

    def _tl_seg_origin(self):
        """窗口内第一段在原列表中的序号（用于色块交替稳定）"""
        segs_w, _ = self._tl_window()
        if not segs_w:
            return 0
        first = segs_w[0]['file']
        for i, s in enumerate(self.ctl.segments()):
            if s['file'] == first:
                return i
        return 0

    def _tl_to_time(self, x):
        w = self.timeline.winfo_width()
        _, t0 = self._tl_window()
        total = max(1.0, self.ctl.timeline_end())
        t1 = max(t0 + 1.0, total)
        return max(t0, min(t1, t0 + max(0.0, min(1.0, x / max(1, w)))
                          * (t1 - t0)))

    def _on_timeline_click(self, e):
        self._tl_dragging = True
        self.ctl.seek(self._tl_to_time(e.x))

    def _on_timeline_drag(self, e):
        if self._tl_dragging:
            self.ctl.preview_seek(self._tl_to_time(e.x))
            self._show_hover(e.x)      # 拖拽时提示跟着手指走

    def _on_timeline_release(self, e):
        if self._tl_dragging:
            self._tl_dragging = False
            self.ctl.seek(self._tl_to_time(e.x))

    # 悬停提示 -----------------------------------------------------------
    def _on_timeline_motion(self, e):
        self._show_hover(e.x)

    def _on_timeline_leave(self, _e):
        self._tl_hover = None
        self._hide_hover()

    def _show_hover(self, x):
        """鼠标在 x 处：算出该位置的录制轴秒数并画提示。"""
        w = max(1, self.timeline.winfo_width())
        x = max(0, min(w - 1, x))
        self._tl_hover = self._tl_to_time(x)
        self._draw_hover_tip(x)

    def _hide_hover(self):
        for i in self._tl_tip.values():
            self.timeline.delete(i)
        self._tl_tip = {}

    def _draw_hover_tip(self, x):
        """在时间轴上画一条竖线 + "几分几秒" 气泡。

        只画 3 个 item（线、底、字），每次移动先删旧的再画新的——
        _draw_timeline 会 delete('all')，所以重绘后由它再补一次。
        """
        c = self.timeline
        self._hide_hover()
        t = self._tl_hover
        if t is None:
            return
        w, h = self._tl_geometry()
        if w <= 1:
            return
        x = max(0, min(w - 1, x))
        ln = c.create_line(x, 0, x, h, fill=T.INFO_BLUE, dash=(3, 2))
        # 先画字、量宽度，再把气泡贴到边：靠右时改为向左展开，不越界
        tip = c.create_text(0, h // 2, anchor='nw', text=fmt_time(t),
                            fill=T.FG_TEXT, font=T.F_TINY)
        bx1, by1, bx2, by2 = c.bbox(tip)
        bw, bh = bx2 - bx1, by2 - by1
        pad = px(4)
        tx0 = x + px(6)
        if tx0 + bw + pad > w:
            tx0 = x - px(6) - bw - pad
        tx0 = max(0, min(w - bw - 2 * pad, tx0))
        ty0 = max(1, h - bh - px(4))
        c.coords(tip, tx0 + pad, ty0)
        bg = c.create_rectangle(tx0, ty0 - px(2), tx0 + bw + 2 * pad,
                                ty0 + bh + px(2),
                                fill=T.BG_PANEL, outline=T.INFO_BLUE)
        c.tag_lower(bg, tip)
        c.tag_lower(ln, bg)
        self._tl_tip = {'bg': bg, 'tip': tip, 'line': ln}

    # ---------- 列表交互 ----------
    def _on_seg_select(self, _e):
        sel = self.seg_list.curselection()
        if sel:
            self.ctl.jump_to_segment(sel[0])

    def _on_mark_select(self, _e):
        sel = self.mark_list.curselection()
        if sel:
            n = len(self.ctl.marks())
            self.ctl.jump_to_mark(n - 1 - sel[0])   # 列表是倒序显示

    # ---------- 刷新（主循环调用）----------
    def _begin_recording(self):
        """开始录制：先问归档位置，再起 RK3588。"""
        if self.ctl.remote_busy or self.ctl.recording:
            return
        dlg = ArchiveDirDialog(self.root, self.ctl)
        if dlg.result is None:
            return                      # 取消：什么都不做
        path, remember = dlg.result
        if remember:
            self.ctl.save_archive_dir(path)
        else:
            self.ctl.set_archive_dir(path)
        self.ctl.start_recording()

    def _maybe_archive_dialog(self):
        """停止后弹归档确认框（控制器查好概况才会返回）。"""
        if self._dialog_open:
            return
        p = self.ctl.poll_archive_prompt()
        if p is None:
            return
        sess, info = p
        self._dialog_open = True
        try:
            dlg = ArchiveConfirmDialog(self.root, self.ctl, sess, info)
            if dlg.result is not None:
                path, remove = dlg.result
                ok = self.ctl.start_archive(path, remove_remote=remove,
                                            sess=sess, total=info['bytes'])
                if not ok:
                    self._flash('归档未能启动（可能已有归档在跑）')
        finally:
            self._dialog_open = False

    def refresh(self):
        self._maybe_archive_dialog()
        st = self.ctl.state()
        # 顶栏
        if st['live_ok']:
            self.badge_live.set('LIVE', T.LIVE_GREEN)
        elif st['live_connecting']:
            self.badge_live.set('连接中', T.WARN_AMBER)
        else:
            self.badge_live.set('离线', T.REC_RED)
        self.badge_fps.set('%.1f fps' % st['live_fps'] if st['live_ok']
                           else '-- fps',
                           T.FG_TEXT if st['live_ok'] else T.FG_MUTED)
        if st['recording']:
            self.badge_rec.set('● 录制中', T.REC_RED)
        else:
            self.badge_rec.set('未录制', T.FG_MUTED)
        if not st['history']:
            # 看历史录像时由 _update_history_ui 决定徽标文字
            self.badge_mode.set('直播' if st['mode'] == 'live' else '回放',
                                T.INFO_BLUE if st['mode'] == 'live'
                                else T.WARN_AMBER)
        # 时间码：主行绝对时钟，副行本场时钟 + 绝对帧号
        tc = self.ctl.timecode()
        parts = tc.split()
        if len(parts) == 3 and parts[0][0].isdigit() and ':' in parts[0]:
            self.lbl_wall.config(text=parts[0])
            self.lbl_tc.config(text='%s  帧 %s' % (parts[1], parts[2]))
            self.lbl_clock.config(text='本场 ' + parts[1])
        else:
            # 本场起点未知（旧录像无 base_ms）：退回只显示相对时钟
            self.lbl_wall.config(text=st['clock'])
            self.lbl_tc.config(text='')
            self.lbl_clock.config(text='本场时钟（无绝对时间锚点）')
        self.lbl_session.config(text=st['session'])
        self.lbl_session_len.config(text='本场时长 %s' % st['session_len'])
        self._update_history_ui(st)

        # 按钮状态。看历史录像时直播仍在后台收，所以录制按钮不禁用。
        hist = st['history']
        self.btn_start.set_enabled(not st['remote_busy'] and not st['recording'])
        self.btn_stop.set_enabled(not st['remote_busy'] and st['recording'])
        self.btn_close.set_enabled(hist)
        in_replay = st['mode'] == 'replay'
        self.btn_play.set_enabled(in_replay)
        self.btn_play.set_text('❚❚ 暂停' if st['playing'] else '▶ 播放')
        self.btn_back.set_enabled(in_replay)
        self.btn_fwd.set_enabled(in_replay)
        for b in (self.btn_s1, self.btn_s2, self.btn_s3):
            b.set_enabled(in_replay)
        self.btn_s1.set_active(st['speed_idx'] == 0 and in_replay)
        self.btn_s2.set_active(st['speed_idx'] == 1 and in_replay)
        self.btn_s3.set_active(st['speed_idx'] == 2 and in_replay)
        # 看历史录像时，直播已在后台，主画面不是直播：「直播」按钮保持
        # 可点（按了 = 退出历史录像回直播），但不高亮。
        self.btn_live.set_active(st['mode'] == 'live' and not hist)
        self.btn_replay.set_active(in_replay)

        # 主画面
        self._render_main()
        self._render_live_pip()

        # 时间轴
        self.lbl_pos.config(text=fmt_time(self.ctl.position_s()))
        self.lbl_dur.config(text=fmt_time(self.ctl.timeline_end()))
        self._draw_timeline()

        # 片段列表
        self._refresh_seg_list(st)
        self._refresh_mark_list()
        # 归档进行中优先展示进度，别让裁判以为卡住了
        if st['archive_busy']:
            tot = max(1, st['archive_total'])
            pct = min(100, int(100.0 * st['archive_done'] / tot))
            phase = {'connect': '连接中', 'transfer': '传输中',
                     'verify': '校验中', 'rename': '收尾中',
                     'cleanup': '清理 RK3588 文件'}.get(
                         st['archive_phase'], st['archive_phase'])
            self.lbl_status.config(
                text='归档 %s：%s  %d%%  (%.1f/%.1f GB) → %s' % (
                    st['archive_session'], phase, pct,
                    st['archive_done'] / 1e9, tot / 1e9,
                    st['archive_dir']))
        elif time.monotonic() - self._flash_t > 4.0:
            # 刚操作过的提示保留 4 秒，别被状态栏刷新立刻覆盖
            self.lbl_status.config(text=st['status'])
        self._clear_toast()

    def _refresh_seg_list(self, st):
        segs = self.ctl.segments()
        n = len(segs)
        # 签名只看内容（数量 + 末段文件名 + 末段结束时刻）：archive 每轮
        # 轮询都生成全新的 dict，用 id() 会让签名每周期都变，列表被反复
        # 重建、裁判滚到的位置被顶回顶部。
        sig = (n, segs[-1]['file'] if n else None,
               round(segs[-1]['end_s'], 1) if n else None)
        if sig != self._seg_sig:
            prev = self._seg_sig
            self._seg_sig = sig
            if n == 0:
                self.seg_list.delete(0, 'end')
                self.seg_list.insert('end', '   尚无已封存片段…')
                self._seg_cur = -1
                return
            # 增量：只是末尾多了一两段 → 追加，别整表重建
            grown = (prev is not None and prev[0] < n
                     and self.seg_list.size() == prev[0])
            if grown:
                for i in range(prev[0], n):
                    s = segs[i]
                    dur = s['end_s'] - s['start_s']
                    self.seg_list.insert(
                        'end', '  %02d   %s – %s   %4.1fs' % (
                            i + 1, fmt_time(s['start_s']),
                            fmt_time(s['end_s']), dur))
            else:
                self.seg_list.delete(0, 'end')
                for i, s in enumerate(segs):
                    dur = s['end_s'] - s['start_s']
                    self.seg_list.insert(
                        'end', '  %02d   %s – %s   %4.1fs' % (
                            i + 1, fmt_time(s['start_s']),
                            fmt_time(s['end_s']), dur))
                self._seg_cur = -1
        cur = self.ctl.current_seg_index()
        if 0 <= cur < n and cur != self._seg_cur:
            self._seg_cur = cur
            self.seg_list.selection_clear(0, 'end')
            self.seg_list.selection_set(cur)
            self.seg_list.see(cur)

    def _refresh_mark_list(self):
        marks = self.ctl.marks()
        # 签名必须带上**是哪一场**：只看条数+末条备注时，新一场恰好也标了
        # 一条「标记 1」，就和上一场撞签名，列表一直挂着旧内容不重建。
        sig = (getattr(self.ctl, 'mark_session', lambda: '')(),
               len(marks),
               tuple((m.get('t_s'), m.get('note')) for m in marks[-3:]))
        if sig == self._mark_sig:
            return
        self._mark_sig = sig
        self.mark_list.delete(0, 'end')
        if not marks:
            self.mark_list.insert('end', '   哨响按 N：记录判罚点 + 备注')
        else:
            for m in reversed(marks):      # 最新的在最上面
                t = m.get('wall_local') or fmt_time(m['t_s'])
                est = '~' if m.get('wall_est') else ' '
                self.mark_list.insert(
                    'end', '  %s⚑ %s  %s' % (est, t, m.get('note', '')))

    def show_confirm(self, text):
        """底部临时提示（复用状态栏或弹出）"""
        self.lbl_status.config(text=text)


# ---------------------------------------------------------------------------
# 录像归档对话框
# ---------------------------------------------------------------------------

class _ModalDialog(tk.Toplevel):
    """深色主题的模态框基类：居中、抓焦点、ESC 关闭"""

    def __init__(self, parent, title, width, height):
        super().__init__(parent)
        self.withdraw()                 # 布局完再显示，避免闪烁跳位
        self.title(title)
        self.configure(bg=T.BG_APP)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result = None
        w, h = px(width), px(height)
        px_r, py_r = parent.winfo_rootx(), parent.winfo_rooty()
        x = px_r + (parent.winfo_width() - w) // 2
        y = py_r + (parent.winfo_height() - h) // 3
        self.geometry('%dx%d+%d+%d' % (w, h, max(0, x), max(0, y)))
        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Escape>', lambda e: self._cancel())

    def _show(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()     # 布局完才抓焦点；构造中途出错就不会留下死 grab
        try:
            self.wait_window()
        finally:
            # 构造过程中出错时 self 会残留在 Tk 里并一直抓着 grab，
            # 主窗口之后点什么都不响应。无论怎么退出都把 grab 交还。
            try:
                self.grab_release()
            except tk.TclError:
                pass

    def _cancel(self):
        self.grab_release()
        self.destroy()

    def _label(self, parent, text, font=T.F_BODY, fg=T.FG_TEXT,
               bg=T.BG_APP, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=bg,
                        justify='left', anchor='w', **kw)


class ArchiveDirDialog(_ModalDialog):
    """开始录制前：选本次录像的归档目录。

    录制期间数据仍只落 RK3588（不依赖网络），这里选的只是「停止后
    拷回哪里」。
    """

    def __init__(self, parent, ctl):
        super().__init__(parent, '本次录像归档位置', 660, 330)
        self.ctl = ctl
        body = tk.Frame(self, bg=T.BG_APP)
        body.pack(fill='both', expand=True, padx=px(22), pady=px(18))

        self._label(body, '本次录像归档位置', T.F_TITLE).pack(anchor='w')
        self._label(
            body,
            '录制期间先存 RK3588 本地（保证网络异常也不丢录像），\n'
            '停止后再自动拷到这个目录。',
            T.F_SMALL, T.FG_MUTED).pack(anchor='w', pady=(px(6), px(16)))

        self._label(body, '目标目录', T.F_SMALL, T.FG_MUTED).pack(anchor='w')
        row = tk.Frame(body, bg=T.BG_APP)
        row.pack(fill='x', pady=(px(4), 0))
        self.ent = tk.Entry(row, font=T.F_BODY, bg=T.BG_RAISED,
                            fg=T.FG_TEXT, insertbackground=T.FG_TEXT,
                            relief='flat', bd=px(8))
        self.ent.insert(0, ctl.archive_dir)
        self.ent.pack(side='left', fill='x', expand=True, ipady=px(6))
        browse = FlatButton(row, '浏览…', self._browse, width=84,
                            height=34, style='normal')
        browse.pack(side='left', padx=(px(8), 0))

        self.remember = tk.BooleanVar(value=True)
        chk = tk.Checkbutton(
            body, text='记住这个位置，下次默认使用', variable=self.remember,
            font=T.F_SMALL, fg=T.FG_MUTED, bg=T.BG_APP,
            activebackground=T.BG_APP, activeforeground=T.FG_TEXT,
            selectcolor=T.BG_RAISED, bd=0, highlightthickness=0)
        chk.pack(anchor='w', pady=(px(14), 0))

        self.err = self._label(body, '', T.F_SMALL, T.REC_RED)
        self.err.pack(anchor='w', pady=(px(8), 0))

        btns = tk.Frame(body, bg=T.BG_APP)
        btns.pack(side='bottom', fill='x', pady=(px(12), 0))
        FlatButton(btns, '取消', self._cancel, width=92, height=38,
                   style='normal').pack(side='right', padx=(px(8), 0))
        FlatButton(btns, '开始录制', self._ok, width=124, height=38,
                   style='primary').pack(side='right')
        self.ent.focus_set()
        self.ent.bind('<Return>', lambda e: self._ok())
        self._show()

    def _browse(self):
        from tkinter import filedialog
        cur = self.ent.get().strip() or self.ctl.archive_dir
        d = filedialog.askdirectory(
            parent=self, title='选择录像归档目录',
            initialdir=cur if os.path.isdir(cur) else os.path.expanduser('~'))
        if d:
            self.ent.delete(0, 'end')
            self.ent.insert(0, d)

    def _ok(self):
        path = self.ent.get().strip()
        if not path:
            self.err.config(text='请填一个目录')
            return
        path = os.path.expanduser(path)
        # 只建目录，不校验可写——真正的失败会由归档时报出来，
        # 提前挡在这里反而可能误伤刚插上还没挂稳的 U 盘
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            self.err.config(text='无法创建目录: %s' % e)
            return
        self.result = (path, self.remember.get())
        self.grab_release()
        self.destroy()


class OpenSessionDialog(_ModalDialog):
    """「打开录像」：从某个目录里挑一场本地录像。

    目录默认用上次打开的那一场所在目录（没有就用归档目录），可「浏览…」
    换到别的盘/U 盘。只把含 meta.jsonl 的子目录列成场次——不靠名字前缀，
    手动拷过来改过名的目录也能打开。
    """

    def __init__(self, parent, ctl, start_dir):
        super().__init__(parent, '打开录像', 700, 460)
        self.ctl = ctl
        self.result = None
        self.dir = (start_dir if start_dir and os.path.isdir(start_dir)
                    else os.path.expanduser('~'))
        body = tk.Frame(self, bg=T.BG_APP)
        body.pack(fill='both', expand=True, padx=px(20), pady=px(16))

        self._label(body, '打开录像', T.F_TITLE).pack(anchor='w')
        self._label(body, '从本机选一场录像回看（不影响正在直播/录制的场次）。',
                    T.F_SMALL, T.FG_MUTED).pack(anchor='w',
                                                pady=(px(4), px(12)))

        row = tk.Frame(body, bg=T.BG_APP)
        row.pack(fill='x')
        self.ent = tk.Entry(row, font=T.F_BODY, bg=T.BG_RAISED,
                            fg=T.FG_TEXT, insertbackground=T.FG_TEXT,
                            relief='flat', bd=px(8))
        self.ent.insert(0, self.dir)
        self.ent.pack(side='left', fill='x', expand=True, ipady=px(6))
        self.ent.bind('<Return>', lambda e: self._reload())
        FlatButton(row, '浏览…', self._browse, width=84, height=34,
                   style='normal').pack(side='left', padx=(px(8), 0))
        FlatButton(row, '刷新', self._reload, width=68, height=34,
                   style='normal').pack(side='left', padx=(px(8), 0))

        self.lbl_dir = self._label(body, '', T.F_TINY, T.FG_DIM)
        self.lbl_dir.pack(anchor='w', pady=(px(6), px(6)))

        lst_frame = tk.Frame(body, bg=T.BG_PANEL)
        lst_frame.pack(fill='both', expand=True)
        self.lst = tk.Listbox(
            lst_frame, bg=T.BG_RAISED, fg=T.FG_TEXT, font=T.F_BODY,
            selectbackground=T.INFO_BLUE_D, selectforeground='#ffffff',
            highlightthickness=0, bd=0, activestyle='none',
            exportselection=False, selectborderwidth=0)
        sb = tk.Scrollbar(lst_frame, orient='vertical',
                          command=self.lst.yview, width=px(12),
                          troughcolor=T.BG_RAISED, bg=T.BG_ACTIVE,
                          activebackground=T.BORDER_LT, relief='flat', bd=0,
                          highlightthickness=0)
        self.lst.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.lst.pack(side='left', fill='both', expand=True)
        self.lst.bind('<Double-1>', lambda e: self._ok())
        self.lst.bind('<Return>', lambda e: self._ok())

        self.lbl_info = self._label(body, '', T.F_SMALL, T.FG_MUTED)
        self.lbl_info.pack(anchor='w', pady=(px(6), 0))

        btns = tk.Frame(body, bg=T.BG_APP)
        btns.pack(side='bottom', fill='x', pady=(px(12), 0))
        FlatButton(btns, '取消', self._cancel, width=92, height=38,
                   style='normal').pack(side='right', padx=(px(8), 0))
        FlatButton(btns, '打开', self._ok, width=104, height=38,
                   style='primary').pack(side='right')

        self._sessions = []
        self.lst.bind('<<ListboxSelect>>', self._on_select)
        self._reload()
        self._show()

    # ---------------------------------------------------------------
    def _reload(self):
        path = os.path.expanduser(self.ent.get().strip())
        self.ent.delete(0, 'end')
        self.ent.insert(0, path)
        self.lst.delete(0, 'end')
        self._sessions = []
        self.lbl_info.config(text='')
        if not os.path.isdir(path):
            self.lbl_dir.config(text='目录不存在')
            return
        self.dir = path
        self.lbl_dir.config(text=path)
        dirs = svc.list_session_dirs(path)
        if not dirs:
            self.lbl_dir.config(text='%s（下面没有录像场次）' % path)
            return
        for d in dirs:
            self._sessions.append(d)
            self.lst.insert('end', '  %s' % self._describe(d))
        self.lst.selection_set(0)
        self._on_select(None)

    def _describe(self, d):
        """一行摘要：场次名 + 时长 + 分段数（不读文件内容，秒回）"""
        name = os.path.basename(d.rstrip('/'))
        n, dur = 0, 0.0
        try:
            with open(os.path.join(d, 'meta.jsonl'), encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith('{'):
                        continue
                    try:
                        r = json.loads(line)
                        dur = max(dur, r['end_ms'] / 1000.0)
                    except (ValueError, KeyError):
                        continue
                    n += 1
        except OSError:
            pass
        return '%-26s %5d 段   %s' % (name, n, fmt_time(dur))

    def _on_select(self, _e):
        sel = self.lst.curselection()
        if not sel or sel[0] >= len(self._sessions):
            self.lbl_info.config(text='')
            return
        d = self._sessions[sel[0]]
        self.lbl_info.config(text='完整路径  %s' % d)

    def _browse(self):
        from tkinter import filedialog
        cur = os.path.expanduser(self.ent.get().strip())
        d = filedialog.askdirectory(
            parent=self, title='选择含录像场次的目录',
            initialdir=cur if os.path.isdir(cur) else os.path.expanduser('~'))
        if d:
            self.ent.delete(0, 'end')
            self.ent.insert(0, d)
            self._reload()

    def _ok(self):
        sel = self.lst.curselection()
        if not sel or sel[0] >= len(self._sessions):
            self.lbl_info.config(text='先选一场录像')
            return
        self.result = self._sessions[sel[0]]
        self.grab_release()
        self.destroy()


class ArchiveConfirmDialog(_ModalDialog):
    """停止录制后：确认归档到哪、要不要删 RK3588 原文件。"""

    def __init__(self, parent, ctl, sess, info):
        super().__init__(parent, '录像已停止', 700, 400)
        self.ctl = ctl
        self.sess = sess
        body = tk.Frame(self, bg=T.BG_APP)
        body.pack(fill='both', expand=True, padx=px(22), pady=px(18))

        self._label(body, '录像已停止', T.F_TITLE,
                    T.LIVE_GREEN).pack(anchor='w')
        self._label(body, sess, T.F_SMALL, T.FG_MUTED).pack(
            anchor='w', pady=(px(2), px(14)))

        secs = info.get('seconds') or 0
        dur = fmt_time(secs) if secs else '未知'
        grid = tk.Frame(body, bg=T.BG_APP)
        grid.pack(fill='x', pady=(0, px(14)))
        for i, (k, v) in enumerate((
                ('分段', '%d 个' % info['segments']),
                ('大小', '%.1f GB' % (info['bytes'] / 1e9)),
                ('时长', dur))):
            self._label(grid, k, T.F_SMALL, T.FG_MUTED).grid(
                row=0, column=i * 2, sticky='w', padx=(0, px(6)))
            self._label(grid, v, T.F_BODY, T.FG_TEXT).grid(
                row=0, column=i * 2 + 1, sticky='w',
                padx=(0, px(24)))

        self._label(body, '归档到', T.F_SMALL, T.FG_MUTED).pack(anchor='w')
        row = tk.Frame(body, bg=T.BG_APP)
        row.pack(fill='x', pady=(px(4), 0))
        self.ent = tk.Entry(row, font=T.F_BODY, bg=T.BG_RAISED,
                            fg=T.FG_TEXT, insertbackground=T.FG_TEXT,
                            relief='flat', bd=px(8))
        self.ent.insert(0, ctl.archive_dir)
        self.ent.pack(side='left', fill='x', expand=True, ipady=px(6))
        FlatButton(row, '浏览…', self._browse, width=84, height=34,
                   style='normal').pack(side='left', padx=(px(8), 0))

        # 删除远端是危险动作：默认不勾，且要裁判主动选
        self.remove = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(
            body, text='归档校验通过后，删除 RK3588 上的原文件（释放空间）',
            variable=self.remove, font=T.F_SMALL, fg=T.WARN_AMBER,
            bg=T.BG_APP, activebackground=T.BG_APP,
            activeforeground=T.FG_TEXT, selectcolor=T.BG_RAISED,
            bd=0, highlightthickness=0)
        chk.pack(anchor='w', pady=(px(14), 0))

        self.err = self._label(body, '', T.F_SMALL, T.REC_RED)
        self.err.pack(anchor='w', pady=(px(6), 0))

        btns = tk.Frame(body, bg=T.BG_APP)
        btns.pack(side='bottom', fill='x', pady=(px(12), 0))
        FlatButton(btns, '稍后再归档', self._later, width=124,
                   height=38, style='normal').pack(side='left')
        FlatButton(btns, '取消', self._cancel, width=92, height=38,
                   style='normal').pack(side='right', padx=(px(8), 0))
        FlatButton(btns, '开始归档', self._ok, width=124, height=38,
                   style='primary').pack(side='right')
        self.ent.focus_set()
        self.ent.bind('<Return>', lambda e: self._ok())
        self._show()

    def _browse(self):
        from tkinter import filedialog
        cur = self.ent.get().strip() or self.ctl.archive_dir
        d = filedialog.askdirectory(
            parent=self, title='选择录像归档目录',
            initialdir=cur if os.path.isdir(cur) else os.path.expanduser('~'))
        if d:
            self.ent.delete(0, 'end')
            self.ent.insert(0, d)

    def _later(self):
        self.ctl.skip_archive()
        self.result = None
        self.grab_release()
        self.destroy()

    def _ok(self):
        path = self.ent.get().strip()
        if not path:
            self.err.config(text='请填一个目录')
            return
        path = os.path.expanduser(path)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            self.err.config(text='无法创建目录: %s' % e)
            return
        self.result = (path, self.remove.get())
        self.grab_release()
        self.destroy()


class LibraryDialog(tk.Toplevel):
    """录像库：汇总本机所有已归档场次，可查看详情、带标记回放、删除。

    与「打开录像」(OpenSessionDialog) 的区别：这是**库**不是文件浏览器。
    列出每场的时长/大小/段数/标记数，选中后右侧显示标记清单，可直接
    打开回放（标记会按当时记录原样呈现，因为标记跟着场次目录走）。

    非阻塞（不用 _ModalDialog 的 wait_window）：删除/刷新常要等后台
    线程，阻塞会让主窗口连同直播一起冻住，裁判就看不到画面了。
    """

    def __init__(self, parent, ctl, on_open):
        super().__init__(parent)
        self.title('录像库')
        self.configure(bg=T.BG_APP)
        self.transient(parent)
        self.ctl = ctl
        self._on_open = on_open        # 回调：打开某场回放
        self._sessions = []
        self._sel = None
        self._busy = False
        self._closing = False

        w, h = px(1080), px(700)
        px_r, py_r = parent.winfo_rootx(), parent.winfo_rooty()
        self.geometry('%dx%d+%d+%d' % (
            w, h,
            max(0, px_r + (parent.winfo_width() - w) // 2),
            max(0, py_r + (parent.winfo_height() - h) // 3)))
        self.minsize(px(860), px(520))
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.bind('<Escape>', lambda e: self._close())

        self._build()
        self.after(60, self._reload)

    # ---------- 布局 ----------
    def _build(self):
        head = tk.Frame(self, bg=T.BG_PANEL, height=px(58))
        head.pack(side='top', fill='x')
        head.pack_propagate(False)
        tk.Label(head, text='录像库', fg=T.FG_TEXT, bg=T.BG_PANEL,
                 font=T.F_TITLE).pack(side='left', padx=px(18))
        self.lbl_count = tk.Label(head, text='正在扫描…', fg=T.FG_MUTED,
                                  bg=T.BG_PANEL, font=T.F_SMALL)
        self.lbl_count.pack(side='left', padx=(px(12), 0))
        FlatButton(head, '刷新', self._reload, width=68, height=32,
                   style='normal').pack(side='right', padx=(px(8), px(14)),
                                        pady=px(13))
        FlatButton(head, '换目录…', self._browse, width=92, height=32,
                   style='normal').pack(side='right', pady=px(13))
        self.lbl_root = tk.Label(head, text='', fg=T.FG_DIM, bg=T.BG_PANEL,
                                 font=T.F_TINY)
        self.lbl_root.pack(side='right', padx=(px(10), px(10)))
        tk.Frame(self, bg=T.BORDER, height=1).pack(side='top', fill='x')

        body = tk.Frame(self, bg=T.BG_APP)
        body.pack(fill='both', expand=True, padx=px(14), pady=px(12))

        # 左：场次列表
        left = tk.Frame(body, bg=T.BG_PANEL)
        left.pack(side='left', fill='both', expand=True)
        self.lst = tk.Listbox(
            left, bg=T.BG_RAISED, fg=T.FG_TEXT, font=T.F_LIB_LIST,
            selectbackground=T.INFO_BLUE_D, selectforeground='#ffffff',
            highlightthickness=0, bd=0, activestyle='none',
            exportselection=False, selectborderwidth=0)
        sb = tk.Scrollbar(left, orient='vertical', command=self.lst.yview,
                          width=px(12), troughcolor=T.BG_RAISED,
                          bg=T.BG_ACTIVE, activebackground=T.BORDER_LT,
                          relief='flat', bd=0, highlightthickness=0)
        self.lst.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.lst.pack(side='left', fill='both', expand=True)
        self.lst.bind('<<ListboxSelect>>', self._on_select)
        self.lst.bind('<Double-1>', lambda e: self._open())
        self.lst.bind('<Return>', lambda e: self._open())

        # 右：详情
        right = tk.Frame(body, bg=T.BG_PANEL, width=px(330))
        right.pack(side='right', fill='y', padx=(px(12), 0))
        right.pack_propagate(False)
        self.lbl_title = tk.Label(right, text='选中一场录像', fg=T.FG_TEXT,
                                  bg=T.BG_PANEL, font=T.F_SUB, anchor='w',
                                  justify='left', wraplength=px(300))
        self.lbl_title.pack(anchor='w', padx=px(14), pady=(px(14), px(6)))
        self.lbl_meta = tk.Label(right, text='', fg=T.FG_MUTED, bg=T.BG_PANEL,
                                 font=T.F_SMALL, anchor='w', justify='left')
        self.lbl_meta.pack(anchor='w', padx=px(14))
        tk.Frame(right, bg=T.BORDER, height=1).pack(
            fill='x', padx=px(14), pady=px(10))
        tk.Label(right, text='判罚标记', fg=T.FG_MUTED, bg=T.BG_PANEL,
                 font=T.F_SMALL, anchor='w').pack(anchor='w', padx=px(14))
        self.lbl_marks = tk.Label(right, text='（无）', fg=T.FG_TEXT,
                                  bg=T.BG_PANEL, font=T.F_SMALL,
                                  anchor='nw', justify='left',
                                  wraplength=px(300))
        self.lbl_marks.pack(anchor='w', fill='both', expand=True,
                            padx=px(14), pady=(px(6), 0))

        btns = tk.Frame(self, bg=T.BG_PANEL, height=px(58))
        btns.pack(side='bottom', fill='x')
        btns.pack_propagate(False)
        self.btn_open = FlatButton(btns, '▶ 打开回放', self._open, width=140,
                                   height=38, style='primary')
        self.btn_open.pack(side='left', padx=px(14), pady=px(10))
        self.btn_del = FlatButton(btns, '删除此场…', self._delete, width=124,
                                  height=38, style='danger')
        self.btn_del.pack(side='left', pady=px(10))
        self.lbl_status = tk.Label(btns, text='', fg=T.FG_MUTED,
                                   bg=T.BG_PANEL, font=T.F_SMALL)
        self.lbl_status.pack(side='right', padx=px(16))

    # ---------- 数据 ----------
    def _say(self, text, color=None):
        if self._closing:
            return
        try:
            self.lbl_status.config(text=text, fg=color or T.FG_MUTED)
        except tk.TclError:
            pass

    def _reload(self):
        if self._closing or self._busy:
            return
        self._busy = True
        roots = list(self.ctl.library_roots())
        self.lbl_root.config(text=roots[0] if roots else '')
        self.lbl_count.config(text='正在扫描…')

        def work():
            try:
                sess = self.ctl.library.list_sessions()
                err = None
            except Exception as e:
                sess, err = [], '%s: %s' % (type(e).__name__, e)
            if self._closing:
                return
            try:
                self.after(0, lambda: self._fill(sess, err))
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, sessions, err):
        self._busy = False
        if self._closing:
            return
        prev = self._sel['dir'] if self._sel else None
        self._sessions = sessions
        self.lst.delete(0, 'end')
        total = 0
        for s in sessions:
            total += s['bytes']
            self.lst.insert('end', '  %-24s %7s  %6s  %3d 标记' % (
                s['name'], fmt_size(s['bytes']), fmt_time(s['seconds']),
                s['marks']))
        self.lbl_count.config(text='%d 场 · 共 %s' % (
            len(sessions), fmt_size(total)))
        if err:
            self._say('扫描出错: %s' % err, T.REC_RED)
        if not sessions:
            self._sel = None
            self.lbl_title.config(text='录像库是空的')
            self.lbl_meta.config(text='归档一场录像后就会出现在这里。')
            self.lbl_marks.config(text='（无）')
            return
        # 尽量保持原选中项
        idx = 0
        for i, s in enumerate(sessions):
            if s['dir'] == prev:
                idx = i
                break
        self.lst.selection_set(idx)
        self.lst.see(idx)
        self._on_select(None)
        self._say('已载入 %d 场' % len(sessions))

    def _on_select(self, _e):
        sel = self.lst.curselection()
        if not sel or sel[0] >= len(self._sessions):
            return
        s = self._sessions[sel[0]]
        self._sel = s
        self.lbl_title.config(text=s['name'])
        bits = ['时长 %s' % fmt_time(s['seconds']),
                '%d 段' % s['segments'],
                fmt_size(s['bytes'])]
        if s.get('mtime'):
            bits.append(time.strftime('%m-%d %H:%M',
                                      time.localtime(s['mtime'])))
        if s.get('missing'):
            bits.append('⚠ %d 段文件缺失' % s['missing'])
        self.lbl_meta.config(text=' · '.join(bits))
        # 标记清单放后台读，长场次几百条标记
        def work():
            try:
                marks = self.ctl.library.read_marks(s['dir'], s['name'])
                txt = self._fmt_marks(marks)
            except Exception as e:
                txt = '读取标记出错: %s' % e
            if self._closing:
                return
            try:
                self.after(0, lambda: self._set_marks(txt))
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _fmt_marks(marks):
        if not marks:
            return '（本场没有标记）'
        lines = []
        for i, m in enumerate(marks, 1):
            t = fmt_time(m.get('t_s') or 0.0)
            note = m.get('note') or ''
            lines.append('%2d. %s  %s' % (i, t, note))
        return '\n'.join(lines)

    def _set_marks(self, txt):
        if self._closing:
            return
        try:
            self.lbl_marks.config(text=txt)
        except tk.TclError:
            pass

    # ---------- 操作 ----------
    def _open(self):
        if not self._sel:
            self._say('先选一场录像', T.WARN_AMBER)
            return
        target = self._sel
        name = self.ctl.open_session(target['dir'])
        if not name:
            self._say('打不开：%s' % target['name'], T.REC_RED)
            return
        self._close()
        self._on_open(name)

    def _delete(self):
        if not self._sel:
            self._say('先选一场录像', T.WARN_AMBER)
            return
        s = self._sel
        from tkinter import messagebox
        msg = ('确定删除这场录像吗？\n\n'
               '场次：%s\n'
               '时长：%s\n'
               '大小：%s（%d 段）\n'
               '标记：%d 条\n\n'
               '删除后无法恢复。' % (
                   s['name'], fmt_time(s['seconds']), fmt_size(s['bytes']),
                   s['segments'], s['marks']))
        if not messagebox.askyesno('删除录像', msg, parent=self,
                                   icon='warning', default='no'):
            return
        self._say('正在删除 %s…' % s['name'], T.WARN_AMBER)
        self.btn_del.set_enabled(False)

        def work():
            try:
                freed = self.ctl.library.delete_session(s['dir'])
                res, err = freed, None
            except Exception as e:
                res, err = 0, '%s: %s' % (type(e).__name__, e)
            if self._closing:
                return
            try:
                self.after(0, lambda: self._after_delete(res, err))
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _after_delete(self, freed, err):
        if self._closing:
            return
        self.btn_del.set_enabled(True)
        if err:
            self._say('删除失败：%s' % err, T.REC_RED)
            return
        self._say('已删除，释放 %s' % fmt_size(freed), T.LIVE_GREEN)
        self._sel = None
        self._reload()

    def _browse(self):
        from tkinter import filedialog
        roots = self.ctl.library_roots()
        cur = roots[0] if roots else os.path.expanduser('~')
        d = filedialog.askdirectory(
            parent=self, title='选择录像库目录',
            initialdir=cur if os.path.isdir(cur) else os.path.expanduser('~'))
        if not d:
            return
        self.ctl.add_library_root(d)
        self._reload()

    def _close(self):
        self._closing = True
        try:
            self.destroy()
        except tk.TclError:
            pass
