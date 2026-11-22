"""Sports replay desk: compact native-aspect views, shared transport and timeline."""
import concurrent.futures
from collections import deque
from datetime import datetime
import json
import logging
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import uuid

import cv2
from PIL import Image, ImageTk

import sports_theme as T
from sports_widgets import RoundedCard,SoftButton,IconBadge,section_heading,transport_state
from studio_services_rk import fmt_hms
from sports_replay import (Track, ReplayCoordinator, camera_catalog, grouped_sessions,
                           measure_clock, save_json)
from match_clock import MatchClock, mmss

LOG=logging.getLogger(__name__)


def tc(seconds):
    seconds=max(0,int(seconds))
    return '%02d:%02d:%02d'%(seconds//3600,seconds//60%60,seconds%60)


class CameraSurface:
    def __init__(self, parent, app, key):
        self.app,self.key=app,key
        self.frame=tk.Frame(parent,bg=T.BORDER,highlightthickness=2,highlightbackground=T.BORDER)
        self.header=tk.Frame(self.frame,bg=T.VIDEO_HEADER)
        self.header.pack(fill='x');self.header.columnconfigure(0,weight=1)
        self.name_label=tk.Label(self.header,text=key.upper(),bg=T.VIDEO_HEADER,fg=T.VIDEO_FG,font=T.F_TITLE,anchor='w')
        self.name_label.grid(row=0,column=0,sticky='ew',padx=(12,4),pady=(3,0))
        self.fps_label=tk.Label(self.header,bg=T.VIDEO_HEADER,fg=T.VIDEO_FG,font=T.F_SMALL,anchor='e')
        self.fps_label.grid(row=0,column=1,sticky='e',padx=(4,12),pady=(3,0))
        self.state_label=tk.Label(self.header,bg=T.VIDEO_HEADER,fg=T.VIDEO_MUTED,font=T.F_SMALL,
                                   anchor='w',justify='left',width=1,wraplength=240)
        self.state_label.grid(row=1,column=0,columnspan=2,sticky='ew',padx=12,pady=(0,5))
        self.header.bind('<Configure>',lambda e:self.state_label.config(wraplength=max(80,e.width-24)))
        self.canvas=tk.Canvas(self.frame,bg=T.VIDEO_BG,highlightthickness=0,
                              highlightbackground=T.BORDER,cursor='hand2')
        self.canvas.pack(fill='both',expand=True)
        self.photo=None;self.last=None;self.signature=None
        self.photo_size=None;self.presented=deque(maxlen=240);self.presented_count=0
        self.paint_ms=deque(maxlen=240);self.image_item=None;self.hud_signature=None
        self.status_time=0;self.status_signature=None
        self.canvas.bind('<Button-1>',lambda e:app.select(key))
        self.canvas.bind('<Double-Button-1>',lambda e:app.focus_camera(key))
        for widget in [self.frame,self.header,self.name_label,self.state_label,self.fps_label]:
            widget.bind('<Button-1>',lambda e:app.select(key))
            widget.bind('<Double-Button-1>',lambda e:app.focus_camera(key))

    def update_status(self):
        now=time.monotonic();c=self.app.ctls[self.key];co=self.app.co
        alive=c.feed.alive();replay=c.mode=='replay'
        signal='在线' if alive else ('信号中断' if c.recording else '未采集')
        recording='● 录制中' if c.recording else '未录制'
        mode='回放' if replay else '直播'
        status=(signal,recording,mode,co.state,self.app.selected,co.messages.get(self.key,''))
        if status==self.status_signature and now-self.status_time<.5:return
        self.status_time=now;self.status_signature=status
        self.frame.config(highlightbackground=T.INFO_BLUE if self.app.selected==self.key else T.BORDER)
        self.state_label.config(text=recording+' · '+signal+' · '+(co.messages.get(self.key,'') or mode),
                                fg='#ff8b9a' if c.recording else T.VIDEO_MUTED)
        self.fps_label.config(text='%.1f fps'%self.display_fps() if co.state not in ('paused','loading') else
                             ('暂停' if co.state=='paused' else '加载中'))

    def draw(self):
        c=self.app.ctls[self.key];co=self.app.co
        self.update_status()
        w,h=self.canvas.winfo_width(),self.canvas.winfo_height()
        if w<4 or h<4:return
        img=c.current_frame()
        note=co.messages.get(self.key,'')
        if co.state!='live' and co.linked and self.key not in co.tracks:
            img=None;note='本赛事未绑定该路录像'
        replay=c.mode=='replay'
        if co.state=='loading' and self.key in co.participants:img=None
        alive=c.feed.alive()
        sig=(id(img),w,h,replay,note,alive,c.recording,self.app.selected,
             int(co.playhead()) if replay else int(c.feed.image_t))
        if sig==self.signature:return
        fresh=img is not None and img is not self.last
        self.signature=sig;self.last=img
        started=time.monotonic()
        canvas=self.canvas
        canvas.config(highlightbackground=T.INFO_BLUE if self.app.selected==self.key else T.BORDER)
        if img is not None:
            ih,iw=img.shape[:2];s=min(w/iw,h/ih)
            nw,nh=max(1,int(iw*s)),max(1,int(ih*s))
            rgb=cv2.cvtColor(cv2.resize(img,(nw,nh)),cv2.COLOR_BGR2RGB)
            image=Image.fromarray(rgb)
            if self.photo is None or self.photo_size!=(nw,nh):
                self.photo=ImageTk.PhotoImage(image,master=canvas);self.photo_size=(nw,nh)
                if self.image_item is None:self.image_item=canvas.create_image(w//2,h//2,image=self.photo)
                else:canvas.itemconfigure(self.image_item,image=self.photo)
            else:self.photo.paste(image)
            canvas.coords(self.image_item,w//2,h//2);canvas.itemconfigure(self.image_item,state='normal')
            canvas.delete('empty')
        else:
            if self.image_item is not None:canvas.itemconfigure(self.image_item,state='hidden')
            canvas.delete('empty')
            text=note or ('等待直播' if c.recording else '未采集')
            canvas.create_text(w//2,h//2,text=text,fill=T.VIDEO_MUTED,font=T.F_BODY,width=max(80,w-30),tags='empty')
        if fresh:
            self.presented.append(time.monotonic());self.presented_count+=1
            self.paint_ms.append((time.monotonic()-started)*1000)

    def display_fps(self):
        now=time.monotonic()
        samples=[t for t in self.presented if now-t<2]
        return (len(samples)-1)/(samples[-1]-samples[0]) if len(samples)>1 else 0.


class SportsMonitor:
    def __init__(self,root,nodes,controllers,state_root,pad_enabled=True):
        cv2.setNumThreads(max(1,min(4,cv2.getNumberOfCPUs())))
        self.root,self.nodes=root,nodes
        self.ctls={n['id']:c for n,c in zip(nodes,controllers)}
        self.controllers=controllers
        self.state_root=Path(state_root)
        self.co=ReplayCoordinator(self.ctls,background=True)
        self.selected=nodes[0]['id'];self.focused=None;self.alignment_reference=0
        self.co.selected=self.selected
        self.ring_busy=False;self.ring_overlays={}
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.results=queue.Queue();self.closing=False
        self.catalogs={};self.groups=[];self.group_labels=[]
        self.match_id=None;self.match_name='未选择赛事';self.active_paths={}
        self.game_clock=MatchClock();self.hover_x=None;self.hover_y=40
        self.events=[];self.calibrations={};self.clock_busy=False;self.last_clock=0
        self.batch_busy=False;self.batch_action='';self.batch_pending=set();self.batch_results={}
        self.catalog_busy=False;self.last_catalog=0;self.pending_new=False
        self.catalog_generation=0
        self.after_stop=False;self.last_save=0;self.last_health=0;self.last_render=0
        self.message='读取录像…'
        self.zoom=1.;self.view_start=0.;self.drag=None;self.drag_resume=True
        self.pads=[]
        self.storage_manager=None
        root.title('RK3588 体育赛事回放平台')
        root.configure(bg=T.BG_APP);root.geometry('1800x1050');root.minsize(1200,800)
        root.protocol('WM_DELETE_WINDOW',self.close)
        try:root.attributes('-zoomed',True)
        except tk.TclError:pass
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('Sports.TCombobox',fieldbackground=T.BG_RAISED,background=T.BG_RAISED,
                         foreground=T.FG_TEXT,arrowcolor=T.FG_TEXT,bordercolor=T.BORDER)
        style.map('Sports.TCombobox',fieldbackground=[('readonly',T.BG_RAISED)],
                  foreground=[('readonly',T.FG_TEXT)],selectbackground=[('readonly',T.BG_RAISED)],
                  selectforeground=[('readonly',T.FG_TEXT)])
        root.option_add('*TCombobox*Listbox.background',T.BG_PANEL)
        root.option_add('*TCombobox*Listbox.foreground',T.FG_TEXT)
        top_card=RoundedCard(root,padding=14)
        top_card.pack(fill='x',padx=12,pady=(10,0))
        top=top_card.body;top.configure(height=62);top.pack_propagate(False)
        IconBadge(top,'camera',size=44,solid=True).pack(side='left',padx=(0,12),pady=5)
        brand=tk.Frame(top,bg=T.BG_PANEL);brand.pack(side='left',padx=(0,22))
        tk.Label(brand,text='赛事回放平台',bg=T.BG_PANEL,fg=T.FG_TEXT,font=T.F_BRAND).pack(anchor='w')
        self.mode_label=tk.Label(top,text='直播',bg=T.INFO_SOFT,fg=T.INFO_BLUE,font=T.F_SMALL,padx=14,pady=7)
        self.mode_label.pack(side='left',padx=10)
        self.top_clock=tk.Label(top,bg=T.BG_PANEL,fg=T.FG_MUTED,font=(T.FONT_MONO,14))
        self.top_clock.pack(side='right',padx=(16,0))
        self.button(top,'四宫格  Esc',self.grid_view).pack(side='right',padx=5,pady=5)
        self.button(top,'录像管理',self.open_storage_manager).pack(side='right',padx=5,pady=5)
        self.header_count=tk.Label(top,text='4 个机位',bg=T.BG_RAISED,fg=T.INFO_BLUE,font=T.F_SMALL,padx=12,pady=7)
        self.header_count.pack(side='right',padx=10)
        bottom_card=RoundedCard(root,padding=12,expand=True)
        bottom_card.configure(height=206)
        bottom_card.pack(side='bottom',fill='x',padx=12,pady=(0,10))
        bottom=bottom_card.body
        toolbar=tk.Frame(bottom,bg=T.BG_PANEL);toolbar.pack(fill='x',pady=(0,3))
        tk.Label(toolbar,text='共同赛事时间轴',font=T.F_TITLE,bg=T.BG_PANEL,fg=T.FG_TEXT).pack(side='left',padx=(0,18))
        self.position_label=tk.Label(toolbar,text='00:00:00 / 00:00:00',font=T.F_HUD,bg=T.BG_PANEL,fg=T.INFO_BLUE)
        self.position_label.pack(side='left')
        self.game_clock_label=tk.Label(toolbar,text='比赛未开始',font=T.F_HUD,bg=T.BG_PANEL,fg=T.FG_MUTED)
        self.game_clock_label.pack(side='left',padx=20)
        self.button(toolbar,'全程',self.fit_timeline).pack(side='right',padx=3)
        self.button(toolbar,'−',lambda:self.change_zoom(1/1.5)).pack(side='right',padx=3)
        self.button(toolbar,'＋',lambda:self.change_zoom(1.5)).pack(side='right',padx=3)
        self.timeline=tk.Canvas(bottom,bg=T.BG_PANEL,height=112,highlightthickness=0)
        self.timeline.pack(fill='both',expand=True,padx=8,pady=3)
        self.timeline.bind('<Button-1>',self.timeline_press)
        self.timeline.bind('<B1-Motion>',self.timeline_motion)
        self.timeline.bind('<ButtonRelease-1>',self.timeline_release)
        self.timeline.bind('<Motion>',self.timeline_hover)
        self.timeline.bind('<Leave>',self.timeline_leave)
        self.timeline.bind('<MouseWheel>',lambda e:self.change_zoom(1.5 if e.delta>0 else 1/1.5))
        self.timeline.bind('<Button-4>',lambda e:self.change_zoom(1.5))
        self.timeline.bind('<Button-5>',lambda e:self.change_zoom(1/1.5))
        self.timeline.bind('<Shift-Button-4>',lambda e:self.pan(-.15))
        self.timeline.bind('<Shift-Button-5>',lambda e:self.pan(.15))
        self.stage=tk.Frame(root,bg=T.BG_APP);self.stage.pack(fill='both',expand=True,padx=12,pady=10)
        self.matrix=tk.Frame(self.stage,bg=T.VIDEO_BG)
        self.side=tk.Frame(self.stage,bg=T.BG_APP)
        self.surfaces={k:CameraSurface(self.matrix,self,k) for k in self.ctls}
        self.stage.bind('<Configure>',self.layout)
        self._sidebar()
        for key,fn in {'<Escape>':self.grid_view,'<space>':self.co.pause,
                        '<Left>':lambda:self.co.step(-1),'<Right>':lambda:self.co.step(1),
                        '<F1>':self.go_live,'<F2>':lambda:self.recent(10),
                        'm':self.add_event}.items():
            root.bind(key,lambda e,fn=fn:self.key_action(fn))
        for ctl in controllers:ctl.start()
        if pad_enabled:
            from referee_data_rk import PadServer
            for ctl in controllers:
                if ctl.args.referee_port:
                    pad=PadServer(ctl,port=ctl.args.referee_port);pad.start();self.pads.append(pad)
        self.refresh_catalog()
        root.after(250,lambda:root.attributes('-zoomed',True))
        root.after(20,self.loop)

    @staticmethod
    def button(parent,text,fn,color=None):
        return SoftButton(parent,text,fn,color=color)

    def _sidebar(self):
        control_wrap=tk.Frame(self.side,bg=T.BG_APP,width=380)
        self.control_wrap=control_wrap
        control_wrap.pack(side='left',fill='y',padx=(0,12));control_wrap.pack_propagate(False)
        style=ttk.Style(self.root)
        style.configure('Sports.Vertical.TScrollbar',background='#c9d8ee',troughcolor=T.BG_APP,
                         bordercolor=T.BG_APP,arrowcolor=T.FG_MUTED,arrowsize=10)
        scroll=ttk.Scrollbar(control_wrap,orient='vertical',style='Sports.Vertical.TScrollbar')
        scroll.pack(side='right',fill='y')
        viewport=tk.Canvas(control_wrap,bg=T.BG_APP,highlightthickness=0,yscrollcommand=scroll.set)
        viewport.pack(side='left',fill='both',expand=True);scroll.config(command=viewport.yview)
        self.controls=tk.Frame(viewport,bg=T.BG_APP)
        content=viewport.create_window((0,0),window=self.controls,anchor='nw',width=350)
        viewport.bind('<Configure>',lambda e:viewport.itemconfigure(content,width=max(1,e.width)))
        self.controls.bind('<Configure>',lambda e:viewport.config(scrollregion=viewport.bbox('all')))
        viewport.bind('<Button-4>',lambda e:viewport.yview_scroll(-3,'units'))
        viewport.bind('<Button-5>',lambda e:viewport.yview_scroll(3,'units'))
        def scroll_controls(e):
            if not str(e.widget).startswith(str(self.controls)) or 'popdown' in str(e.widget):return
            if isinstance(e.widget,(ttk.Combobox,tk.Listbox)):return
            delta=getattr(e,'delta',0)
            direction=(-1 if delta>0 else 1) if delta else (-1 if e.num==4 else 1)
            viewport.yview_scroll(direction*3,'units')
            return 'break'
        for sequence in ['<MouseWheel>','<Button-4>','<Button-5>']:
            self.root.bind_all(sequence,scroll_controls,add='+')
        self.info=tk.Frame(self.side,bg=T.BG_APP)
        self.info.pack(side='left',fill='both',expand=True)

        def card(title,kind,color=T.INFO_BLUE,subtitle=None):
            panel=RoundedCard(self.controls,padding=14)
            panel.pack(fill='x',pady=(0,10))
            section_heading(panel.body,title,kind,color=color)
            return panel.body

        def row(parent,buttons):
            frame=tk.Frame(parent,bg=parent.cget('bg'));frame.pack(fill='x',pady=3)
            for text,fn,color in buttons:
                self.button(frame,text,fn,color).pack(side='left',fill='x',expand=True,padx=2)
            return frame

        recording=card('赛事录制','camera',subtitle='统一启停，保留每个机位的原始录像')
        self.match_combo=ttk.Combobox(recording,state='readonly',style='Sports.TCombobox',font=T.F_SMALL)
        self.match_combo.pack(fill='x',pady=(1,6),ipady=4)
        self.match_combo.bind('<<ComboboxSelected>>',self.choose_match)
        row(recording,[('刷新赛事',self.refresh_catalog,None),('组合录像',self.combine_dialog,None)])
        self.button(recording,'录像与存储管理',self.open_storage_manager).pack(fill='x',pady=(3,4))
        rec_row=tk.Frame(recording,bg=T.BG_PANEL);rec_row.pack(fill='x',pady=(9,4))
        self.start_button=self.button(rec_row,'● 全部开始',lambda:self.batch('start'),T.INFO_BLUE_D)
        self.start_button.pack(side='left',fill='x',expand=True,padx=(0,4))
        self.stop_button=self.button(rec_row,'■ 全部停止',self.confirm_stop,T.REC_RED_D)
        self.stop_button.pack(side='left',fill='x',expand=True)
        self.rec_label=tk.Label(recording,text='录制状态读取中',bg=T.BG_PANEL,fg=T.FG_MUTED,font=T.F_SMALL,anchor='w')
        self.rec_label.pack(fill='x',pady=(5,0))
        game_row=tk.Frame(recording,bg=T.BG_PANEL);game_row.pack(fill='x',pady=(10,0))
        self.game_start_button=self.button(game_row,'比赛开始',self.start_game_clock,T.LIVE_GREEN_D)
        self.game_start_button.pack(side='left',fill='x',expand=True,padx=(0,4))
        self.game_pause_button=self.button(game_row,'暂停比赛',self.toggle_game_clock)
        self.game_pause_button.pack(side='left',fill='x',expand=True)

        playback=card('多机位回放','play',subtitle='共用一个赛事时刻，定位关键画面')
        self.linked=tk.BooleanVar(value=True)
        tk.Checkbutton(playback,text='四路联动',variable=self.linked,command=self.toggle_link,
                        bg=T.BG_PANEL,fg=T.INFO_BLUE,selectcolor=T.INFO_SOFT,
                        activebackground=T.BG_PANEL,activeforeground=T.INFO_BLUE,
                        font=T.F_BODY,bd=0,highlightthickness=0).pack(anchor='w',pady=(0,5))
        row(playback,[('直播',self.go_live,None),('回看10秒',lambda:self.recent(10),None),('回看30秒',lambda:self.recent(30),None)])
        transport=tk.Frame(playback,bg=T.BG_PANEL);transport.pack(fill='x',pady=3)
        self.button(transport,'◀ 单帧',lambda:self.co.step(-1)).pack(side='left',fill='x',expand=True,padx=2)
        self.play_button=SoftButton(transport,'播放',self.co.pause,color=T.INFO_BLUE_D,symbol='play')
        self.play_button.pack(side='left',fill='x',expand=True,padx=2)
        self.button(transport,'单帧 ▶',lambda:self.co.step(1)).pack(side='left',fill='x',expand=True,padx=2)
        rates=tk.Frame(playback,bg=T.BG_PANEL);rates.pack(fill='x',pady=(7,3))
        self.speed_buttons={}
        for speed in [.25,.5,1.,2.]:
            button=self.button(rates,'%gx'%speed,lambda s=speed:self.co.set_speed(s))
            button.pack(side='left',fill='x',expand=True,padx=2);self.speed_buttons[speed]=button
        self.link_status=tk.Label(playback,bg=T.INFO_SOFT,fg=T.FG_MUTED,font=T.F_SMALL,
                                  anchor='w',justify='left',wraplength=300,padx=9,pady=7)
        self.link_status.pack(fill='x',pady=(7,0))

        selected=card('所选机位','camera',color=T.MARK_PURPLE)
        self.selected_label=tk.Label(selected,text=self.selected.upper(),bg=T.BG_PANEL,
                                      fg=T.INFO_BLUE,font=T.F_BODY,anchor='w',justify='left')
        self.selected_label.pack(fill='x',pady=(0,6))
        row(selected,[('放大/还原',lambda:self.focus_camera(self.selected) if self.focused is None else self.grid_view(),None),
                       ('截图',lambda:self.ctls[self.selected].snapshot(),None)])
        row(selected,[('单路开始',lambda:self.batch('start',[self.selected]),None),('单路停止',self.confirm_single_stop,None)])
        row(selected,[('时间对齐',self.align_dialog,None),('归档选中',self.archive_selected,None)])

        timing=card('时间校准','clock',color='#29a89d')
        self.button(timing,'校准设备时钟',self.calibrate).pack(fill='x',pady=(0,5))
        self.clock_label=tk.Label(timing,text='未校准',bg=T.BG_PANEL,
                                  fg=T.FG_MUTED,font=T.F_SMALL,wraplength=300,justify='left',anchor='w')
        self.clock_label.pack(fill='x')
        notice=RoundedCard(self.controls,fill='#fff8e9',padding=12,shadow=False)
        notice.pack(fill='x',pady=(0,8))
        self.notice=tk.Label(notice.body,text=self.message,bg='#fff8e9',fg=T.WARN_AMBER,
                             font=T.F_SMALL,wraplength=300,justify='left',anchor='nw')
        self.notice.pack(fill='x')

        events=RoundedCard(self.info,padding=14,expand=True)
        events.pack(fill='both',expand=True)
        section_heading(events.body,'赛事事件','mark',color=T.MARK_PURPLE)
        entry_row=tk.Frame(events.body,bg=T.BG_PANEL);entry_row.pack(fill='x',pady=(1,8))
        self.note=tk.Entry(entry_row,bg=T.BG_RAISED,fg=T.FG_TEXT,insertbackground=T.INFO_BLUE,
                            font=T.F_BODY,relief='flat',bd=8)
        self.note.bind('<Return>',lambda e:self.add_event())
        mark_button=self.button(entry_row,'＋ 标记时刻',self.add_event,T.INFO_BLUE_D)
        entry_mode=[None]
        def arrange_entry(e):
            stacked=e.width<280
            if entry_mode[0]==stacked:return
            entry_mode[0]=stacked;mark_button.pack_forget();self.note.pack_forget()
            if stacked:
                self.note.pack(fill='x',ipady=2)
                mark_button.pack(fill='x',pady=(6,0))
            else:
                mark_button.pack(side='right',padx=(7,0))
                self.note.pack(side='left',fill='x',expand=True,ipady=2)
        entry_row.bind('<Configure>',arrange_entry)
        actions=tk.Frame(events.body,bg=T.BG_PANEL);actions.pack(side='bottom',fill='x',pady=(8,0))
        for text,fn in [('跳转',self.jump_event),('编辑',self.edit_event),('删除',self.delete_event),('导出',self.export_events)]:
            self.button(actions,text,fn).pack(side='left',fill='x',expand=True,padx=2)
        event_area=tk.Frame(events.body,bg=T.BG_RAISED);event_area.pack(fill='both',expand=True)
        event_scroll=ttk.Scrollbar(event_area,orient='vertical',style='Sports.Vertical.TScrollbar')
        self.event_list=tk.Listbox(event_area,bg=T.BG_RAISED,fg=T.FG_TEXT,font=T.F_BODY,
                                   selectbackground=T.INFO_SOFT,selectforeground=T.INFO_BLUE,
                                    relief='flat',highlightthickness=0,exportselection=False,yscrollcommand=event_scroll.set)
        self.event_list.pack(side='left',fill='both',expand=True,padx=8,pady=8)
        event_scroll.pack(side='right',fill='y');event_scroll.config(command=self.event_list.yview)
        self.event_list.bind('<Double-Button-1>',lambda e:self.jump_event())
        self.event_list.bind('<F2>',lambda e:self.edit_event())
        self.event_empty=tk.Label(event_area,text='暂无标记',bg=T.BG_RAISED,
                                   fg=T.FG_MUTED,font=T.F_TITLE,justify='center',wraplength=240)
        self.event_area=event_area
        event_area.bind('<Configure>',self.update_event_empty)


    def key_action(self,fn):
        if self.root.grab_current() is not None:return
        focus=self.root.focus_get()
        if isinstance(focus,(tk.Entry,ttk.Combobox,tk.Text)):return
        fn();return 'break'

    def layout(self,event=None):
        w,h=self.stage.winfo_width(),self.stage.winfo_height()
        if w<4 or h<4:return
        aspect=2448/2048
        mw=min(h*aspect,w-600)
        mh=mw/aspect
        self.matrix.place(x=0,y=int((h-mh)/2),width=int(mw),height=int(mh))
        side_width=max(590,w-int(mw)-8)
        self.side.place(x=int(mw)+8,y=0,width=side_width,height=h)
        if hasattr(self,'control_wrap'):self.control_wrap.configure(width=420 if side_width>900 else 340 if side_width>650 else 300)
        keys=list(self.ctls)
        for i,key in enumerate(keys):
            canvas=getattr(self.surfaces[key],'frame',self.surfaces[key].canvas)
            if self.focused is not None:
                if key!=self.focused:canvas.place_forget();continue
                # Tk place retains omitted options and ADDS relative sizes to
                # absolute sizes. Reset both in either direction of the switch.
                canvas.place(x=0,y=0,width=int(mw),height=int(mh),relwidth=0,relheight=0)
            else:
                canvas.place(x=int((i%2)*mw/2),y=int((i//2)*mh/2),
                              width=int(mw/2)-1,height=int(mh/2)-1,relwidth=0,relheight=0)

    def select(self,key):
        self.selected=key;self.co.selected=key

    def focus_camera(self,key):
        self.select(key);self.focused=key;self.layout()

    def grid_view(self):
        if self.root.grab_current() is None:self.focused=None;self.layout()

    def job(self,kind,fn,context=None):
        future=self.pool.submit(fn)
        def completed(f):
            try:self.results.put((kind,context,f.result(),None))
            except Exception as exc:
                LOG.exception('%s failed',kind)
                self.results.put((kind,context,None,str(exc)))
        future.add_done_callback(completed)

    def refresh_catalog(self):
        if self.catalog_busy:return
        manager=getattr(self,'storage_manager',None)
        if manager and manager.busy:return
        self.catalog_busy=True;self.last_catalog=time.monotonic()
        def fetch():
            result={}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures={pool.submit(camera_catalog,n,self.ctls[n['id']].archive_dir):n['id'] for n in self.nodes}
                for f,key in futures.items():
                    try:result[key]=f.result()
                    except Exception:LOG.exception('Catalog %s unavailable',key)
            return result
        self.job('catalog',fetch,self.catalog_generation)

    def apply_catalog(self,result):
        self.catalog_busy=False
        self.catalogs.update(result)
        if not self.batch_busy:
            for key,sessions in result.items():
                ctl=self.ctls[key]
                if ctl.remote_busy:continue
                if sessions and all(s.get('node_offline') for s in sessions):continue
                active=any(s.get('active',False) for s in sessions)
                if active and not ctl.recording:ctl.feed.set_want(True)
                ctl.recording=active
        self.groups=grouped_sessions(self.catalogs)
        self.group_labels=[]
        for group in self.groups:
            start=min(t.start for t in group.values());duration=max(t.end for t in group.values())-start
            self.group_labels.append('%s · %d路 · %s'%(datetime.fromtimestamp(start).strftime('%m-%d %H:%M'),len(group),tc(duration)))
        self.match_combo['values']=self.group_labels
        if self.pending_new:
            tracks={}
            for key,directory in self.active_paths.items():
                s=next((s for s in self.catalogs.get(key,[]) if s['dir']==directory),None)
                if s and s['segments']:
                    t=Track.from_session(key,s)
                    if key in self.calibrations:
                        t.calibrated=True;t.clock_samples=[self.calibrations[key]]
                        t.uncertainty=self.calibrations[key]['uncertainty']
                    tracks[key]=t
            if tracks:
                self.co.set_tracks(tracks);self.pending_new=len(tracks)<len(self.active_paths)
                self.save_match()
        elif self.co.tracks:
            # Refresh only the explicitly bound source; never follow a newer folder.
            for key,track in self.co.tracks.items():
                s=next((s for s in self.catalogs.get(key,[]) if s['dir']==track.directory),None)
                if s:track.segments=sorted(s['segments']+self.ring_overlays.get(key,[]),key=lambda r:r['start_s'])
            if self.after_stop:
                self.after_stop=False
                self.co.recent(10,False)
            self.save_match()
        elif self.groups:
            active={k:s['dir'] for k,sessions in self.catalogs.items() for s in sessions if s.get('active') and s['segments']}
            index=next((i for i,g in enumerate(self.groups) if active and
                        {k:t.directory for k,t in g.items()}==active),0)
            self.use_group(self.groups[index],self.group_labels[index])
            self.match_combo.current(index)
            if not any(c.recording for c in self.controllers):
                start=max(t.start for t in self.co.tracks.values())-self.co.origin+.5
                self.co.seek(start,False)
        if not self.groups and not self.co.tracks:self.message='暂无录像'
        elif not self.batch_busy:self.message='就绪'

    def use_group(self,tracks,name):
        self.ring_overlays.clear()
        for ctl in self.controllers:ctl.to_live()
        self.match_id='match_'+uuid.uuid4().hex[:12]
        self.match_name=name;self.events=[]
        self.game_clock=MatchClock();self.timeline_leave()
        self.co.set_tracks(tracks);self.fit_timeline()
        # Restore prior per-match calibration and events when the same four
        # directories are opened again, independently of the combo ordering.
        signature={k:t.directory for k,t in tracks.items()}
        for p in (self.state_root/'matches').glob('*.json'):
            try:
                saved=json.loads(p.read_text())
                saved_paths=dict(saved.get('recording_paths',{}))
                saved_paths.update({k:v['directory'] for k,v in saved['tracks'].items()})
                if saved_paths==signature:
                    self.match_id=saved['id'];self.events=saved.get('events',[])
                    self.match_name=saved.get('name',name)
                    self.game_clock=MatchClock(saved.get('match_clock'))
                    for key,t in tracks.items():
                        v=saved['tracks'].get(key,{})
                        calibration=saved.get('clock_calibrations',{}).get(key)
                        if not v.get('clock_samples') and calibration:
                            v=dict(v,calibrated=True,clock_samples=[calibration],uncertainty=calibration['uncertainty'])
                        for field in ['offset','calibrated','uncertainty','clock_samples']:
                            if field in v:setattr(t,field,v[field])
                    self.co.origin=min((t.start for t in tracks.values() if t.segments),default=0);break
            except (OSError,ValueError,KeyError):continue
        self.save_match();self.update_events()

    def choose_match(self,event=None):
        i=self.match_combo.current()
        if 0<=i<len(self.groups):
            self.pending_new=False;self.use_group(self.groups[i],self.group_labels[i])
            start=max(t.start for t in self.co.tracks.values())-self.co.origin+.5
            self.co.seek(start,False)

    def save_match(self):
        if not self.match_id:return
        tracks={k:t.dump() for k,t in self.co.tracks.items()}
        for track in tracks.values():track['segments']=[s for s in track['segments'] if not s.get('ring')]
        paths={k:t['directory'] for k,t in tracks.items()}
        if getattr(self,'pending_new',False):paths.update(self.active_paths)
        save_json(self.state_root/'matches'/(self.match_id+'.json'),
                  dict(id=self.match_id,name=self.match_name,tracks=tracks,events=self.events,
                       match_clock=self.game_clock.dump() if getattr(self,'game_clock',None) else None,
                       recording_paths=paths,
                       clock_calibrations=getattr(self,'calibrations',{}) if getattr(self,'pending_new',False) else {}))

    def current_match_recording(self):
        if not self.match_id:return False
        for key,ctl in self.ctls.items():
            if not ctl.recording:continue
            track=self.co.tracks.get(key)
            active=next((s['dir'] for s in self.catalogs.get(key,[]) if s.get('active')),None)
            if track and track.directory==active:return True
            if self.pending_new and key in self.active_paths:return True
        return False

    def start_game_clock(self):
        if self.game_clock.started:return
        if not self.current_match_recording():
            messagebox.showinfo('比赛计时','请先开启本场录像，再开始比赛。',parent=self.root);return
        # Use the current DGX wall time, never the replay pointer (which may be
        # displaying older video). Clock correction already maps tracks here.
        self.game_clock.start();self.save_match();self.update_events();self.update_game_clock_ui()
        self.message='比赛开始'

    def toggle_game_clock(self):
        if not self.game_clock.started:return
        if self.game_clock.running:self.game_clock.pause();self.message='比赛已暂停'
        else:
            if not self.current_match_recording():
                messagebox.showinfo('比赛计时','请先恢复本场录像。',parent=self.root);return
            self.game_clock.resume();self.message='比赛继续'
        self.save_match();self.update_events();self.update_game_clock_ui()

    def update_game_clock_ui(self):
        clock=self.game_clock
        text=('比赛 '+mmss(clock.current()) if clock.running else '比赛暂停 '+mmss(clock.current())) if clock.started else '比赛未开始'
        self.game_clock_label.config(text=text,fg=T.LIVE_GREEN if clock.running else T.WARN_AMBER if clock.started else T.FG_MUTED)
        self.game_start_button.config(state='normal' if not clock.started and not self.batch_busy and self.current_match_recording() else 'disabled')
        self.game_pause_button.config(text='暂停比赛' if clock.running else '继续比赛',
                                     state='normal' if clock.started and (clock.running or self.current_match_recording()) else 'disabled')

    def match_time_label(self,position,explicit=False):
        clock=getattr(self,'game_clock',None)
        if not clock or not clock.started:return ('录像 ' if explicit else '')+tc(position)
        if self.co.duration<=0 or not self.co.tracks:return '等待录像'
        return clock.label_at(self.co.origin+position)

    def combine_dialog(self):
        dialog=tk.Toplevel(self.root);dialog.title('组合四路赛事录像');dialog.configure(bg=T.BG_PANEL)
        dialog.transient(self.root);choices={}
        for node in self.nodes:
            key=node['id'];sessions=self.catalogs.get(key,[])
            tk.Label(dialog,text=key.upper(),bg=T.BG_PANEL,fg=T.FG_TEXT).pack(anchor='w',padx=15,pady=(10,3))
            combo=ttk.Combobox(dialog,state='readonly',width=48,style='Sports.TCombobox')
            combo['values']=['不参与本场']+[s['name'] for s in sessions]
            chosen=self.co.tracks.get(key)
            index=next((i+1 for i,s in enumerate(sessions) if chosen and s['dir']==chosen.directory),0)
            combo.current(index);combo.pack(padx=15,pady=3);choices[key]=combo
        def apply():
            tracks={}
            for key,combo in choices.items():
                i=combo.current()-1
                if i>=0:tracks[key]=Track.from_session(key,self.catalogs[key][i])
            if not tracks:return
            self.use_group(tracks,'手动组合赛事');self.match_combo.set('手动组合赛事')
            self.co.seek(max(t.start for t in tracks.values())-self.co.origin+.5,False)
            dialog.destroy()
        self.button(dialog,'载入这组录像',apply).pack(fill='x',padx=15,pady=15)
        dialog.grab_set()

    def confirm_stop(self):
        if self.batch_busy:return
        if messagebox.askyesno('全部停止录像','停止四台相机的当前录制？\n录像会安全收尾并保留，之后仍可回放。',parent=self.root):self.batch('stop')

    def confirm_single_stop(self):
        if messagebox.askyesno('停止所选相机','停止 %s 的当前录制？'%self.selected.upper(),parent=self.root):self.batch('stop',[self.selected])

    def batch(self,action,keys=None):
        if self.batch_busy:return
        manager=getattr(self,'storage_manager',None)
        if manager and manager.busy:
            self.message='录像管理正在传输、校验或删除，请完成后再启停录制。';return
        keys=list(keys or self.ctls)
        if any(self.ctls[k].remote_busy for k in keys):
            self.message='所选设备正在执行其他录制操作，请稍候。';return
        if action=='start' and all(self.ctls[k].recording for k in keys):
            self.message='所选相机已在录制，无需重复启动。';return
        self.batch_busy=True;self.batch_action=action;self.batch_pending=set(keys);self.batch_results={}
        if action=='stop':
            self.stopping_paths={key:(next((s['dir'] for s in self.catalogs.get(key,[]) if s.get('active')),None)
                                      or self.active_paths.get(key)) for key in keys}
        self.message='正在%s %d 台相机…'%('启动' if action=='start' else '安全停止',len(keys))
        if action=='start' and len(keys)==4:
            if getattr(self,'game_clock',None) and self.game_clock.running:self.game_clock.pause();self.save_match()
            self.ring_overlays.clear()
            self.pending_new=True;self.active_paths={}
            self.match_id='match_'+datetime.now().strftime('%Y%m%d_%H%M%S')
            self.match_name=datetime.now().strftime('%m-%d %H:%M')+' 新赛事'
            self.game_clock=MatchClock();self.timeline_leave()
            self.events=[];self.co.live();self.co.set_tracks({});self.match_combo.set(self.match_name)
            self.calibrate()
        for key in keys:
            ctl=self.ctls[key]
            ctl.remote_busy=True
            def work(key=key,ctl=ctl):
                if action=='start':
                    # Preserve the prior runtime log before the recorder's
                    # normal launcher overwrites /tmp/hk_record.log.
                    if not ctl.recording:
                        ctl.remote._ssh('d=$(ls -1dt %s/stage1_* 2>/dev/null | head -1); '
                            'if [ -n "$d" ] && [ -f /tmp/hk_record.log ]; then '
                            'cp -n /tmp/hk_record.log "$d/record-runtime.log"; fi'%ctl.args.remote_root)
                    status,detail=ctl.remote.start()
                    directory=''
                    if status in ('STARTED','ALREADY'):
                        sessions=ctl.remote._ssh('ls -1dt %s/stage1_* 2>/dev/null | head -1'%ctl.args.remote_root)
                        directory=sessions.stdout.strip()
                    return status,detail,directory
                status,detail=ctl.remote.stop()
                return status,detail,''
            self.job('batch',work,key)

    def batch_result(self,key,result,error):
        ctl=self.ctls[key];ctl.remote_busy=False
        self.batch_pending.discard(key)
        status,detail,directory=result if result is not None else ('FAIL',error,'')
        success=status in ('STARTED','ALREADY','STOPPED','NO_PROC')
        self.batch_results[key]=(success,status,detail)
        if success:
            ctl.recording=self.batch_action=='start'
            ctl.feed.set_want(ctl.recording)
            if directory:self.active_paths[key]=directory
        if self.batch_pending:return
        self.batch_busy=False
        ok=sum(v[0] for v in self.batch_results.values())
        self.message='%s完成 %d/%d。'%('启动' if self.batch_action=='start' else '停止',ok,len(self.batch_results))
        for k,(passed,status,detail) in self.batch_results.items():
            if not passed:self.message+='\n%s：%s'%(k.upper(),detail[:150])
        LOG.info('Batch %s results: %s',self.batch_action,self.batch_results)
        if self.batch_action=='stop':
            self.after_stop=True
            if not any(c.recording for c in self.controllers):self.freeze_stopped_match_clocks()
        if self.pending_new and not self.active_paths:self.pending_new=False
        self.refresh_catalog()

    def freeze_stopped_match_clocks(self):
        stopped={p for p in getattr(self,'stopping_paths',{}).values() if p}
        if not stopped:return
        # The operator may be reviewing another match when all recorders stop.
        # Freeze the actual stopped match's saved clock as well as the visible one.
        for p in (self.state_root/'matches').glob('*.json'):
            try:
                data=json.loads(p.read_text());paths=set(data.get('recording_paths',{}).values())
                paths.update(t['directory'] for t in data.get('tracks',{}).values())
                if not paths.intersection(stopped):continue
                if data.get('id')==self.match_id:
                    if self.game_clock.pause():self.save_match()
                elif data.get('match_clock'):
                    clock=MatchClock(data['match_clock'])
                    if clock.pause():data['match_clock']=clock.dump();save_json(p,data)
            except (OSError,ValueError,KeyError):LOG.exception('Could not freeze stopped match clock: %s',p)

    def calibrate(self):
        if self.clock_busy:return
        self.clock_busy=True
        def work():
            result={}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures={pool.submit(measure_clock,n):n['id'] for n in self.nodes}
                for f,key in futures.items():
                    try:result[key]=f.result()
                    except Exception:LOG.exception('Clock %s failed',key)
            return result
        self.job('clocks',work)

    def apply_clocks(self,result):
        self.clock_busy=False;self.calibrations=result;self.last_clock=time.monotonic()
        self.clock_label.config(text='设备时间 %d/4'%len(result))
        if self.pending_new or any(c.recording for c in self.controllers):
            for key,track in self.co.tracks.items():
                if key in result and track.calibrated:
                    track.clock_samples.append(result[key]);track.uncertainty=result[key]['uncertainty']
            self.save_match()

    def align_dialog(self):
        key=self.selected;track=self.co.tracks.get(key)
        if track is None:return
        if self.co.linked:self.alignment_reference=self.co.playhead()
        if self.co.state=='playing':self.co.pause()
        dialog=tk.Toplevel(self.root);dialog.title('四路画面时间对齐');dialog.configure(bg=T.BG_PANEL)
        dialog.transient(self.root)
        text='所选：%s\n历史录像的设备时钟未同步。先用单路模式找到同一个动作，\n再把该画面对齐到共同播放指针；校准只保存到赛事清单。'%key.upper()
        tk.Label(dialog,text=text,bg=T.BG_PANEL,fg=T.FG_TEXT,justify='left').pack(padx=16,pady=12)
        def adjust(delta):
            self.co.pause() if self.co.state=='playing' else None
            track.offset+=delta;track.calibrated=True
            self.rebase_alignment()
            self.save_match();self.co.seek(self.co.playhead(),False)
        row=tk.Frame(dialog,bg=T.BG_PANEL);row.pack(padx=16,pady=5)
        for label,delta in [('前移1帧',1/59.2),('后移1帧',-1/59.2),('前移0.1秒',.1),('后移0.1秒',-.1)]:
            self.button(row,label,lambda d=delta:adjust(d)).pack(side='left',padx=3)
        def align_current():
            wall=track.wall_for_position(self.ctls[key].position_s())
            if wall is None:return
            track.offset+=wall-(self.co.origin+self.alignment_reference);track.calibrated=True
            self.rebase_alignment()
            self.save_match();self.co.seek(self.alignment_reference,False)
        self.button(dialog,'将该路当前帧对齐到共同指针',align_current).pack(fill='x',padx=16,pady=5)
        def numeric():
            v=simpledialog.askfloat('手动校准','节点时间减去共同时间（秒）：',initialvalue=track.offset,parent=dialog)
            if v is not None:
                track.offset=v;track.calibrated=True;self.rebase_alignment();self.save_match();self.co.seek(self.co.playhead(),False)
        self.button(dialog,'输入校准秒数…',numeric).pack(fill='x',padx=16,pady=5)
        self.button(dialog,'完成',dialog.destroy).pack(fill='x',padx=16,pady=12)
        dialog.grab_set()

    def rebase_alignment(self):
        delta=self.co.rebase()
        self.alignment_reference+=delta
        for event in self.events:event['position']+=delta
        self.update_events()

    def archive_selected(self):
        self.open_storage_manager()

    def archive_dialog(self,key,name,info):
        from studio_ui_rk import ArchiveConfirmDialog
        ctl=self.ctls[key]
        dialog=ArchiveConfirmDialog(self.root,ctl,name,info)
        if dialog.result:
            destination,remove=dialog.result
            ctl.start_archive(destination,remove_remote=remove,sess=name,total=info['bytes'])

    def open_storage_manager(self):
        from storage_manager import StorageManager
        if self.storage_manager and not self.storage_manager.closed:
            self.storage_manager.window.deiconify();self.storage_manager.window.lift();return
        self.storage_manager=StorageManager(self)

    def storage_before_delete(self,rows):
        self.catalog_generation+=1
        removed={r['directory'] for r in rows}
        if any(t.directory in removed for t in self.co.tracks.values()):
            self.co.live()
            for ctl in self.controllers:
                ctl.loader.clear_pending();ctl.close_session(quiet=True)
        self.message='正在按所选副本位置删除录像…'

    def storage_after_operation(self,action,outcomes,snapshot):
        if action!='delete':
            self.refresh_catalog();return
        deleted={(r['node'],r['directory']):r for r,success,detail in outcomes if success}
        if not deleted:
            self.refresh_catalog();return
        alternatives={(r['node'],r['name']):r for r in snapshot['rows'] if r['segments']}

        def update_tracks(tracks):
            for key,track in list(tracks.items()):
                directory=track['directory']
                if (key,directory) not in deleted:continue
                other=alternatives.get((key,track['name']))
                if other:
                    track['directory']=other['directory']
                    track['segments']=[dict(s,**({'local_dir':other['directory']} if other['location']=='dgx' else {}))
                                       for s in other['segments']]
                else:del tracks[key]

        def origin(tracks):
            return min((Track(**t).start for t in tracks.values() if t['segments']),default=0)

        # Persisted calibration/events survive a switch from RK to DGX.
        for p in (self.state_root/'matches').glob('*.json'):
            try:
                saved=json.loads(p.read_text());before=json.dumps(saved.get('tracks',{}),sort_keys=True)
                old_origin=origin(saved['tracks'])
                update_tracks(saved['tracks'])
                if 'recording_paths' in saved:
                    for key,directory in list(saved['recording_paths'].items()):
                        row=deleted.get((key,directory))
                        if row:
                            other=alternatives.get((key,row['name']))
                            if other:saved['recording_paths'][key]=other['directory']
                            else:del saved['recording_paths'][key]
                if before!=json.dumps(saved['tracks'],sort_keys=True):
                    if saved['tracks']:
                        delta=old_origin-origin(saved['tracks'])
                        for event in saved.get('events',[]):event['position']+=delta
                    save_json(p,saved)
            except (OSError,ValueError,KeyError):LOG.exception('Update deleted recording reference: %s',p)
        current={key:t.dump() for key,t in self.co.tracks.items()}
        changed=any((key,t.directory) in deleted for key,t in self.co.tracks.items())
        old_origin=origin(current)
        update_tracks(current)
        if changed:
            if current:
                delta=old_origin-origin(current)
                for event in self.events:event['position']+=delta
            self.co.set_tracks({k:Track(**v) for k,v in current.items()})
            self.ring_overlays.clear()
            self.update_events()
        for (key,directory),row in deleted.items():
            loader=self.ctls[key].loader
            with loader.cv:
                for segment in row['segments']:
                    name=segment['file'];loader.clips.pop(name,None);loader.bad.discard(name)
                    loader.errors.pop(name,None);loader.fail_count.pop(name,None)
            # Only this session's generated cache files are eligible for cleanup.
            root=Path(loader.cache_dir).resolve()
            for p in root.glob(row['name']+'__*'):
                if p.is_file() and not p.is_symlink() and p.resolve().parent==root:
                    try:p.unlink()
                    except OSError:LOG.warning('Could not remove replay cache %s',p)
            self.catalogs[key]=[s for s in self.catalogs.get(key,[]) if s['dir']!=directory]
        self.save_match();self.refresh_catalog()
        self.message='录像副本已删除；保留的另一处副本可继续回放。'

    def go_live(self):
        self.ring_overlays.clear()
        active={}
        for key,ctl in self.ctls.items():
            directory=self.active_paths.get(key) or ctl.archive.session_name()
            session=next((s for s in self.catalogs.get(key,[]) if s['dir']==directory),None)
            if session and ctl.recording:active[key]=Track.from_session(key,session)
            track=self.co.tracks.get(key)
            source=next((s for s in self.catalogs.get(key,[]) if track and s['dir']==track.directory),None)
            if source:track.segments=source['segments']
        if active and any(k not in self.co.tracks or self.co.tracks[k].directory!=t.directory for k,t in active.items()):
            self.use_group(active,'当前录制赛事');self.match_combo.set('当前录制赛事')
        self.co.live();self.message='直播'

    def recent(self,seconds):
        if not self.co.tracks:
            self.message='等待首个录像分段封存，或先选择一场赛事。';return
        if self.co.state=='live' and any(c.recording for c in self.controllers):
            if self.ring_busy:return
            self.ring_busy=True
            generation=self.co.generation
            keys=list(self.co.participants)
            self.message='后台获取四路最近 %d 秒，直播继续…'%seconds
            def work():
                from multi_monitor import fetch_recent
                import shutil
                def fetch(key):
                    ctl=self.ctls[key];node=next(n for n in self.nodes if n['id']==key)
                    base=ctl.archive.base_ms()
                    if base is None:raise RuntimeError('等待首段时间锚点')
                    parent=self.state_root/key/'ring-cache';parent.mkdir(parents=True,exist_ok=True)
                    old=sorted(parent.glob('recent_*'),key=lambda p:p.stat().st_mtime,reverse=True)
                    for p in old[3:]:
                        if p.is_dir():shutil.rmtree(p)
                    directory=parent/('recent_'+uuid.uuid4().hex)
                    name,info=fetch_recent(node,seconds,directory,base)
                    rec=json.loads((directory/'meta.jsonl').read_text())
                    start=(rec['base_ms']-base)/1000
                    duration=rec['end_ms']/1000
                    rec.update(start_ms=start*1000,end_ms=(start+duration)*1000,
                               start_s=start,end_s=start+duration,dir=str(directory),
                               local_dir=str(directory),ring=True)
                    info['start_s']=start
                    return rec,info
                result={};errors={}
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    futures={pool.submit(fetch,key):key for key in keys}
                    for f,key in futures.items():
                        try:result[key]=f.result()
                        except Exception as exc:errors[key]=str(exc)
                return result,errors
            self.job('ring',work,(generation,seconds))
            return
        self.co.recent(seconds,True)

    def toggle_link(self):
        position=self.co.playhead();resume=self.co.state=='playing'
        self.co.linked=self.linked.get()
        if not self.co.linked:self.alignment_reference=position
        if self.co.state!='live':self.co.seek(position,resume)

    def add_event(self):
        if not self.co.tracks:return
        pos=self.co.playhead() if self.co.state!='live' else max(0,min(t.end for t in self.co.tracks.values())-self.co.origin)
        note=self.note.get().strip() or '赛事标记'
        self.events.append(dict(position=pos,note=note));self.events.sort(key=lambda e:e['position'])
        self.note.delete(0,'end');self.save_match();self.update_events()

    def update_events(self):
        self.event_list.delete(0,'end')
        for e in self.events:self.event_list.insert('end',self.match_time_label(e['position'])+'  '+e['note'])
        self.update_event_empty()

    def update_event_empty(self,event=None):
        width,height=self.event_area.winfo_width(),self.event_area.winfo_height()
        if not self.events and height>110 and width>100:
            self.event_empty.configure(wraplength=max(70,min(240,width-24)))
            self.event_empty.place(relx=.5,rely=.5,anchor='center')
        else:self.event_empty.place_forget()

    def jump_event(self):
        sel=self.event_list.curselection()
        if sel:self.co.seek(self.events[sel[0]]['position'],False)

    def delete_event(self):
        sel=self.event_list.curselection()
        if sel:self.events.pop(sel[0]);self.save_match();self.update_events()

    def edit_event(self):
        selected=self.event_list.curselection()
        if not selected:
            self.message='请先在赛事事件列表中选中一个标记，再编辑备注。'
            return 'break'
        target=self.events[selected[0]];match_id=self.match_id
        note=simpledialog.askstring('编辑赛事标记',
            '标记时间：%s\n修改备注（保留原标记时间）：'%self.match_time_label(target['position']),
            initialvalue=target['note'],parent=self.root)
        if note is None:return 'break'
        note=note.strip()
        if not note:
            messagebox.showinfo('备注不能为空','原备注已保留，请输入备注后再保存。',parent=self.root)
            return 'break'
        if self.match_id!=match_id or not any(e is target for e in self.events):
            self.message='赛事或标记已变化，本次编辑未保存，请重新选择。'
            return 'break'
        target['note']=note
        self.save_match();self.update_events()
        index=next(i for i,e in enumerate(self.events) if e is target)
        self.event_list.selection_set(index);self.event_list.activate(index);self.event_list.see(index)
        self.message='备注已保存'
        return 'break'

    def export_events(self):
        path=filedialog.asksaveasfilename(parent=self.root,title='导出赛事标记',defaultextension='.json',filetypes=[('JSON','*.json')])
        if path:save_json(path,dict(match=self.match_name,events=self.events,match_clock=self.game_clock.dump()))

    def timeline_range(self):
        total=max(1,self.co.duration);span=max(.5,total/self.zoom)
        self.view_start=max(0,min(total-span,self.view_start))
        return self.view_start,span

    def fit_timeline(self):
        self.zoom=1.;self.view_start=0.

    def change_zoom(self,factor):
        center=self.co.playhead()
        self.zoom=max(1,min(1000,self.zoom*factor))
        span=max(1,self.co.duration)/self.zoom
        self.view_start=max(0,center-span/2)

    def pan(self,fraction):
        start,span=self.timeline_range();self.view_start=start+span*fraction

    def timeline_time(self,x):
        start,span=self.timeline_range()
        return max(0,min(self.co.duration,start+(x-52)/max(1,self.timeline.winfo_width()-70)*span))

    def timeline_press(self,e):
        self.drag=self.timeline_time(e.x);self.drag_resume=self.co.state!='paused'
        self.timeline_hover(e)

    def timeline_motion(self,e):
        self.drag=self.timeline_time(e.x)
        self.timeline_hover(e)

    def timeline_release(self,e):
        target=self.timeline_time(e.x);self.drag=None
        if self.co.tracks:self.co.seek(target,self.drag_resume)

    def timeline_hover(self,e):
        if e.x<52 or e.x>self.timeline.winfo_width()-18:
            self.timeline_leave();return
        self.hover_x=e.x;self.hover_y=e.y;self.draw_timeline_hover()

    def timeline_leave(self,e=None):
        self.hover_x=None;self.timeline.delete('hover')

    def draw_timeline_hover(self):
        canvas=self.timeline;canvas.delete('hover')
        x=getattr(self,'hover_x',None)
        if x is None:return
        p=self.timeline_time(x);label=self.match_time_label(p,True)
        clock=getattr(self,'game_clock',None)
        if clock and clock.started and not label.startswith(('赛前','暂停','等待')):label='比赛 '+label
        canvas.create_line(x,20,x,108,fill=T.FG_TEXT,dash=(3,3),tags='hover')
        text=canvas.create_text(x,max(20,min(80,getattr(self,'hover_y',40)-24)),text=label,
                                font=T.F_SMALL,fill=T.VIDEO_FG,tags='hover')
        bounds=canvas.bbox(text)
        if bounds:
            if bounds[0]<10:canvas.move(text,10-bounds[0],0)
            elif bounds[2]>canvas.winfo_width()-10:canvas.move(text,canvas.winfo_width()-10-bounds[2],0)
            a,b,c,d=canvas.bbox(text)
            box=canvas.create_rectangle(a-7,b-4,c+7,d+4,fill=T.VIDEO_HEADER,outline='',tags='hover')
            canvas.tag_lower(box,text)

    def draw_timeline(self):
        canvas=self.timeline;w=canvas.winfo_width()
        x0=52;x1=w-18;start,span=self.timeline_range()
        if x1<=x0:return
        def x(pos):return x0+(pos-start)/span*(x1-x0)
        signature=(w,start,span,self.co.origin,self.selected,
                   tuple((k,id(t.segments),len(t.segments),t.offset,
                          tuple((s['remote'],s['offset']) for s in t.clock_samples)) for k,t in self.co.tracks.items()),
                   tuple(e['position'] for e in self.events),
                   len(self.game_clock.transitions) if getattr(self,'game_clock',None) else 0)
        redraw=signature!=getattr(self,'timeline_signature',None)
        if redraw:canvas.delete('all');self.timeline_signature=signature
        for i in range(9) if redraw else []:
            p=start+span*i/8;xx=x(p)
            canvas.create_text(xx,9,text=self.match_time_label(p),fill=T.FG_MUTED,font=T.F_TINY,
                               anchor='e' if i==8 else 'w' if i==0 else 'center')
            canvas.create_line(xx,19,xx,105,fill=T.BORDER)
        for i,node in enumerate(self.nodes if redraw else []):
            key=node['id'];y=23+i*21
            canvas.create_text(6,y+7,text=key.upper(),fill=T.INFO_BLUE if key==self.selected else T.FG_TEXT,anchor='w',font=T.F_SMALL)
            canvas.create_rectangle(x0,y,x1,y+14,fill=T.BG_RAISED,outline='')
            track=self.co.tracks.get(key)
            if track:
                for a,b in track.intervals():
                    left,right=max(x0,x(a-self.co.origin)),min(x1,x(b-self.co.origin))
                    if right>left:canvas.create_rectangle(left,y,right,y+14,fill=T.TRACK_SELECTED if key==self.selected else T.TRACK_BLUE,outline='')
        for event in self.events if redraw else []:
            xx=x(event['position'])
            if x0<=xx<=x1:canvas.create_polygon(xx-4,20,xx+4,20,xx,26,fill=T.WARN_AMBER)
        clock=getattr(self,'game_clock',None)
        if redraw and clock and clock.started:
            zero=x(clock.transitions[0]['wall']-self.co.origin)
            if x0<=zero<=x1:canvas.create_line(zero,18,zero,108,fill=T.LIVE_GREEN,width=2)
            for a,b in clock.paused_intervals(self.co.origin+self.co.duration):
                left,right=max(x0,x(a-self.co.origin)),min(x1,x(b-self.co.origin))
                if right>left:canvas.create_rectangle(left,20,right,108,fill=T.FG_MUTED,stipple='gray50',outline='')
        pos=self.drag if self.drag is not None else self.co.playhead()
        if self.co.state=='live' and self.drag is None:pos=self.co.duration
        xx=x(pos)
        canvas.delete('playhead')
        if x0<=xx<=x1:
            canvas.create_line(xx,16,xx,108,fill=T.INFO_BLUE,width=2,tags='playhead')
            canvas.create_polygon(xx-5,16,xx+5,16,xx,23,fill=T.INFO_BLUE,tags='playhead')
        self.position_label.config(text='%s / %s%s'%(self.match_time_label(pos),self.match_time_label(self.co.duration),'  · 定位中' if self.co.state=='loading' else ''))
        self.draw_timeline_hover()

    def drain(self):
        while True:
            try:kind,context,result,error=self.results.get_nowait()
            except queue.Empty:break
            if kind=='catalog':
                self.catalog_busy=False
                if context!=self.catalog_generation:continue
                if result is not None:self.apply_catalog(result)
                elif error:self.message='读取赛事失败：'+error
            elif kind=='batch':self.batch_result(context,result,error)
            elif kind=='clocks':
                self.clock_busy=False
                if result is not None:self.apply_clocks(result)
            elif kind=='archive-info':
                if result is not None:
                    key,name=context
                    self.root.after(1,lambda k=key,n=name,i=result:self.archive_dialog(k,n,i))
                elif error:self.message='读取归档信息失败：'+error
            elif kind=='ring':
                self.ring_busy=False
                generation,seconds=context
                if generation!=self.co.generation:continue
                if error:self.message='最近回放失败：'+error;continue
                snapshots,errors=result
                ends=[]
                for key,(seg,info) in snapshots.items():
                    if key not in self.co.tracks:continue
                    track=self.co.tracks[key]
                    self.ring_overlays[key]=[seg]
                    track.segments=sorted([s for s in track.segments if not s.get('ring')]+[seg],key=lambda s:s['start_s'])
                    with self.ctls[key].loader.cv:self.ctls[key].loader.clips[seg['file']]=info
                    ends.append(track.bounds(seg)[1])
                if ends:self.co.seek(max(0,min(ends)-self.co.origin-seconds),True)
                self.message='最近回放已就绪 %d/%d 路；完整赛事时间轴保留。'%(len(snapshots),len(self.co.participants))
                for key,text in errors.items():self.message+='\n'+key.upper()+'：'+text

    def write_health(self):
        save_json(self.state_root/'multi-health.json',dict(updated=time.time(),
            platform='sports',view=self.focused or 'grid',state=self.co.state,
            linked=self.co.linked,position=self.co.playhead(),duration=self.co.duration,
            match=self.match_id,batch_busy=self.batch_busy,
            match_clock=self.game_clock.dump(),match_elapsed=self.game_clock.current(),
            nodes={k:dict(alive=c.feed.alive(),fps=c.live_fps(),image_n=c.feed.image_n,
                         recording=c.recording,mode=c.mode,paused=c.paused,position=c.position_s(),
                         source=self.co.tracks[k].directory if k in self.co.tracks else None,
                         status=self.co.messages.get(k,''),display_fps=self.surfaces[k].display_fps(),
                         presented=self.surfaces[k].presented_count) for k,c in self.ctls.items()}))

    def loop(self):
        if self.closing:return
        now=time.monotonic()
        try:
            self.drain();self.co.tick()
            if self.co.state=='live':
                for ctl in self.controllers:ctl.tick()
            for pad in self.pads:pad.pump()
            for surface in self.surfaces.values():
                if self.focused is None or surface.key==self.focused:surface.draw()
            if now-self.last_render>.2:
                self.last_render=now
                self.draw_timeline()
                self.update_game_clock_ui()
                modes={'live':'直播','playing':'联动回放' if self.co.linked else '单路回放','paused':'回放已暂停','loading':'正在共同定位…','buffering':'正在缓冲下一分段…'}
                self.mode_label.config(text=modes[self.co.state],fg=T.LIVE_GREEN if self.co.state=='live' else T.WARN_AMBER)
                symbol,label=transport_state(self.co.state,self.co.resume_after_load)
                self.play_button.config(symbol=symbol,text=label)
                self.top_clock.config(text=datetime.now().strftime('%Y-%m-%d  %H:%M:%S'))
                self.selected_label.config(text=self.selected.upper())
                count=sum(c.recording for c in self.controllers)
                self.rec_label.config(text='正在录制 %d/4 台%s'%(count,' · 执行中…' if self.batch_busy else ''))
                self.header_count.config(text='4 个机位  ·  %d 路录制'%count)
                for b in [self.start_button,self.stop_button]:b.config(state='disabled' if self.batch_busy else 'normal')
                for speed,b in self.speed_buttons.items():b.config(bg=T.INFO_BLUE_D if self.co.speed==speed else T.BG_RAISED)
                calibrated=sum(t.calibrated for t in self.co.tracks.values())
                self.link_status.config(text=('四路联动' if self.co.linked else self.selected.upper())+' · '+
                    ('已校准' if calibrated==len(self.co.tracks) and calibrated else '待对齐'))
                self.notice.config(text=self.message)
            if now-self.last_catalog>5 and not self.catalog_busy:self.refresh_catalog()
            if any(c.recording for c in self.controllers) and now-self.last_clock>60 and not self.clock_busy:self.calibrate()
            if now-self.last_health>5:self.last_health=now;self.write_health()
        except Exception:
            LOG.exception('Sports monitor loop failed')
        # Include processing time in the poll budget; do not add 15 ms after
        # decoding/rendering. Poll above source rate to avoid cadence aliasing.
        elapsed=time.monotonic()-now
        self.root.after(max(1,int((1/240-elapsed)*1000)),self.loop)

    def close(self):
        if self.closing:return
        manager=getattr(self,'storage_manager',None)
        if manager and manager.busy:
            manager.window.lift();self.message='录像管理任务进行中，请完成后再退出。';return
        if manager and not manager.closed:manager.close()
        self.closing=True
        self.co.shutdown()
        self.save_match()
        for pad in self.pads:pad.stop()
        for ctl in self.controllers:ctl.shutdown()
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.root.destroy()
