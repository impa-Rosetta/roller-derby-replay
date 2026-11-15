#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛事监看台视觉规范：深色专业主题（裁判台/转播间质感）

配色思路：近黑底 + 高对比功能色，长时间盯屏不刺眼；
红色=录制/停止，绿色=直播/在线，琥珀=警告/回放，蓝=信息/选中。
"""

# ---- 背景层次（由深到浅）----
BG_APP      = '#0d1117'   # 应用底色
BG_PANEL    = '#161b22'   # 面板底
BG_RAISED   = '#1c2129'   # 卡片 / 按钮底
BG_HOVER    = '#262c36'   # 悬停
BG_ACTIVE   = '#30363d'   # 按下 / 选中底
BORDER      = '#30363d'   # 分隔线
BORDER_LT   = '#3d444d'

# ---- 文字 ----
FG_TEXT     = '#e6edf3'   # 主文字
FG_MUTED    = '#8b949e'   # 次要文字
FG_DIM      = '#6e7681'   # 更弱

# ---- 功能色 ----
LIVE_GREEN  = '#3fb950'   # 直播 / 在线
LIVE_GREEN_D= '#238636'
REC_RED     = '#f85149'   # 录制 / 停止 / 危险
REC_RED_D   = '#da3633'
WARN_AMBER  = '#d29922'   # 警告 / 暂停
INFO_BLUE   = '#58a6ff'   # 信息 / 选中
INFO_BLUE_D = '#1f6feb'
MARK_PURPLE = '#bc8cff'   # 标记

# ---- 字体（Tk 字体族，Linux 常用无衬线）----
FONT_FAMILY   = 'DejaVu Sans'
FONT_MONO     = 'DejaVu Sans Mono'

F_TITLE   = (FONT_FAMILY, 13, 'bold')
F_SUB     = (FONT_FAMILY, 10)
F_BODY    = (FONT_FAMILY, 11)
F_SMALL   = (FONT_FAMILY, 9)
F_TINY    = (FONT_FAMILY, 8)
F_BTN     = (FONT_FAMILY, 11, 'bold')
F_BTN_BIG = (FONT_FAMILY, 13, 'bold')
F_CLOCK   = (FONT_MONO, 26, 'bold')
F_HUD     = (FONT_MONO, 12, 'bold')
F_BADGE   = (FONT_FAMILY, 9, 'bold')
# 录像库场次列表：等宽字体让时长/大小按列对齐
F_LIB_LIST = (FONT_MONO, 9)
