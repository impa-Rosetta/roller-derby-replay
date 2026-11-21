"""Sports replay desk: compact native-aspect views, shared transport and timeline."""
import concurrent.futures
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

import studio_theme as T
from studio_services_rk import fmt_hms
from sports_replay import (Track, ReplayCoordinator, camera_catalog, grouped_sessions,
                           measure_clock, save_json)

LOG=logging.getLogger(__name__)


def tc(seconds):
    seconds=max(0,int(seconds))
    return '%02d:%02d:%02d'%(seconds//3600,seconds//60%60,seconds%60)


class CameraSurface:
    def __init__(self, parent, app, key):
        self.app,self.key=app,key
        self.canvas=tk.Canvas(parent,bg='#05080c',highlightthickness=1,
                              highlightbackground=T.BORDER,cursor='hand2')
        self.photo=None;self.last=None;self.signature=None
        self.canvas.bind('<Button-1>',lambda e:app.select(key))
        self.canvas.bind('<Double-Button-1>',lambda e:app.focus_camera(key))

    def draw(self):
        c=self.app.ctls[self.key];co=self.app.co
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
        self.signature=sig;self.last=img
        canvas=self.canvas;canvas.delete('all')
        canvas.config(highlightbackground=T.INFO_BLUE if self.app.selected==self.key else T.BORDER)
        if img is not None:
            ih,iw=img.shape[:2];s=min(w/iw,h/ih)
            nw,nh=max(1,int(iw*s)),max(1,int(ih*s))
            rgb=cv2.cvtColor(cv2.resize(img,(nw,nh)),cv2.COLOR_BGR2RGB)
            if self.photo is None:self.photo=tk.PhotoImage(master=canvas)
            self.photo.configure(width=nw,height=nh)
            self.photo.put(b'P6 %d %d 255\n'%(nw,nh)+rgb.tobytes(),to=(0,0))
            canvas.create_image(w//2,h//2,image=self.photo)
        else:
            text=note or ('等待相机直播' if c.recording else '录像已停止 · 可选择赛事回放')
            canvas.create_text(w//2,h//2,text=text,fill=T.FG_MUTED,font=T.F_BODY,width=max(80,w-30))
        canvas.create_rectangle(0,0,w,27,fill='#101722',outline='')
        canvas.create_text(12,13,text=self.key.upper(),fill=T.FG_TEXT,anchor='w',font=T.F_TITLE)
        status=('REC ●' if c.recording else '已停止')+'  '+('回放' if replay else '直播')
        canvas.create_text(w-10,13,text=status,fill=T.REC_RED if c.recording else T.FG_MUTED,
                           anchor='e',font=T.F_SMALL)
        if img is not None:
            text=note or (tc(co.playhead()) if replay else '%.1f fps'%c.live_fps())
            if not replay and not alive:text='直播中断 · 等待恢复'
            canvas.create_rectangle(0,h-25,w,h,fill='#101722',outline='')
            canvas.create_text(10,h-13,text=text,anchor='w',fill=T.FG_TEXT,font=T.F_SMALL)


class SportsMonitor:
    def __init__(self,root,nodes,controllers,state_root,pad_enabled=True):
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
        self.events=[];self.calibrations={};self.clock_busy=False;self.last_clock=0
        self.batch_busy=False;self.batch_action='';self.batch_pending=set();self.batch_results={}
        self.catalog_busy=False;self.last_catalog=0;self.pending_new=False
        self.after_stop=False;self.last_save=0;self.last_health=0;self.last_render=0
        self.message='正在读取四台设备的赛事录像索引…'
        self.zoom=1.;self.view_start=0.;self.drag=None;self.drag_resume=True
        self.pads=[]
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
        top=tk.Frame(root,bg=T.BG_PANEL,height=42);top.pack(fill='x');top.pack_propagate(False)
        tk.Label(top,text='赛事回放  /  RK3588',bg=T.BG_PANEL,fg=T.FG_TEXT,font=T.F_TITLE).pack(side='left',padx=16)
        self.mode_label=tk.Label(top,text='直播',bg=T.BG_PANEL,fg=T.LIVE_GREEN,font=T.F_TITLE)
        self.mode_label.pack(side='left',padx=18)
        self.top_clock=tk.Label(top,bg=T.BG_PANEL,fg=T.FG_TEXT,font=T.F_HUD)
        self.top_clock.pack(side='right',padx=16)
        self.button(top,'四宫格 · Esc',self.grid_view).pack(side='right',padx=6,pady=5)
        bottom=tk.Frame(root,bg=T.BG_PANEL,height=164);bottom.pack(side='bottom',fill='x');bottom.pack_propagate(False)
        toolbar=tk.Frame(bottom,bg=T.BG_PANEL);toolbar.pack(fill='x',padx=10,pady=(5,0))
        self.position_label=tk.Label(toolbar,text='00:00:00 / 00:00:00',font=T.F_HUD,bg=T.BG_PANEL,fg=T.FG_TEXT)
        self.position_label.pack(side='left')
        self.button(toolbar,'全程',self.fit_timeline).pack(side='right',padx=3)
        self.button(toolbar,'−',lambda:self.change_zoom(1/1.5)).pack(side='right',padx=3)
        self.button(toolbar,'＋',lambda:self.change_zoom(1.5)).pack(side='right',padx=3)
        self.timeline=tk.Canvas(bottom,bg='#101722',height=112,highlightthickness=0)
        self.timeline.pack(fill='both',expand=True,padx=8,pady=3)
        self.timeline.bind('<Button-1>',self.timeline_press)
        self.timeline.bind('<B1-Motion>',self.timeline_motion)
        self.timeline.bind('<ButtonRelease-1>',self.timeline_release)
        self.timeline.bind('<MouseWheel>',lambda e:self.change_zoom(1.5 if e.delta>0 else 1/1.5))
        self.timeline.bind('<Button-4>',lambda e:self.change_zoom(1.5))
        self.timeline.bind('<Button-5>',lambda e:self.change_zoom(1/1.5))
        self.timeline.bind('<Shift-Button-4>',lambda e:self.pan(-.15))
        self.timeline.bind('<Shift-Button-5>',lambda e:self.pan(.15))
        self.stage=tk.Frame(root,bg=T.BG_APP);self.stage.pack(fill='both',expand=True,padx=8,pady=7)
        self.matrix=tk.Frame(self.stage,bg='#030609')
        self.side=tk.Frame(self.stage,bg=T.BG_PANEL)
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
        return tk.Button(parent,text=text,command=fn,font=T.F_BODY,relief='flat',bd=0,
                          bg=color or T.BG_RAISED,fg=T.FG_TEXT,activebackground=T.BG_HOVER,
                          activeforeground=T.FG_TEXT,padx=8,pady=7,cursor='hand2')

    def _sidebar(self):
        control_wrap=tk.Frame(self.side,bg=T.BG_PANEL,width=350)
        control_wrap.pack(side='left',fill='y',padx=6,pady=10);control_wrap.pack_propagate(False)
        scroll=tk.Scrollbar(control_wrap,orient='vertical')
        scroll.pack(side='right',fill='y')
        viewport=tk.Canvas(control_wrap,bg=T.BG_PANEL,highlightthickness=0,yscrollcommand=scroll.set)
        viewport.pack(side='left',fill='both',expand=True);scroll.config(command=viewport.yview)
        self.controls=tk.Frame(viewport,bg=T.BG_PANEL)
        viewport.create_window((0,0),window=self.controls,anchor='nw',width=330)
        self.controls.bind('<Configure>',lambda e:viewport.config(scrollregion=viewport.bbox('all')))
        viewport.bind('<Button-4>',lambda e:viewport.yview_scroll(-3,'units'))
        viewport.bind('<Button-5>',lambda e:viewport.yview_scroll(3,'units'))
        self.info=tk.Frame(self.side,bg=T.BG_PANEL)
        self.info.pack(side='left',fill='both',expand=True,padx=(0,12),pady=10)
        def title(parent,text):
            tk.Label(parent,text=text,bg=T.BG_PANEL,fg=T.FG_TEXT,font=T.F_TITLE,anchor='w').pack(fill='x',pady=(4,8))
        title(self.controls,'赛事控制')
        self.match_combo=ttk.Combobox(self.controls,state='readonly',style='Sports.TCombobox',font=T.F_SMALL)
        self.match_combo.pack(fill='x',pady=3)
        self.match_combo.bind('<<ComboboxSelected>>',self.choose_match)
        row=tk.Frame(self.controls,bg=T.BG_PANEL);row.pack(fill='x',pady=3)
        self.button(row,'刷新赛事',self.refresh_catalog).pack(side='left',fill='x',expand=True,padx=(0,3))
        self.button(row,'组合录像…',self.combine_dialog).pack(side='left',fill='x',expand=True)
        row=tk.Frame(self.controls,bg=T.BG_PANEL);row.pack(fill='x',pady=(10,3))
        self.start_button=self.button(row,'● 全部开始录像',lambda:self.batch('start'),T.LIVE_GREEN_D)
        self.start_button.pack(side='left',fill='x',expand=True,padx=(0,4))
        self.stop_button=self.button(row,'■ 全部停止',self.confirm_stop,T.REC_RED_D)
        self.stop_button.pack(side='left',fill='x',expand=True)
        self.rec_label=tk.Label(self.controls,text='录制状态读取中',bg=T.BG_PANEL,fg=T.FG_MUTED,font=T.F_SMALL,anchor='w')
        self.rec_label.pack(fill='x',pady=(3,10))
        title(self.controls,'回放控制')
        self.linked=tk.BooleanVar(value=True)
        tk.Checkbutton(self.controls,text='四路联动（取消后仅控制选中相机）',variable=self.linked,
                        command=self.toggle_link,bg=T.BG_PANEL,fg=T.FG_TEXT,
                        selectcolor=T.BG_RAISED,activebackground=T.BG_PANEL,
                        activeforeground=T.FG_TEXT,font=T.F_SMALL).pack(anchor='w',pady=3)
        for buttons in [[('返回直播',self.go_live),('最近 10 秒',lambda:self.recent(10)),('最近 30 秒',lambda:self.recent(30))],
                        [('◀ 单帧',lambda:self.co.step(-1)),('播放 / 暂停',self.co.pause),('单帧 ▶',lambda:self.co.step(1))]]:
            row=tk.Frame(self.controls,bg=T.BG_PANEL);row.pack(fill='x',pady=3)
            for text,fn in buttons:self.button(row,text,fn).pack(side='left',fill='x',expand=True,padx=2)
        row=tk.Frame(self.controls,bg=T.BG_PANEL);row.pack(fill='x',pady=3)
        self.speed_buttons={}
        for speed in [.25,.5,1.,2.]:
            b=self.button(row,'%gx'%speed,lambda s=speed:self.co.set_speed(s))
            b.pack(side='left',fill='x',expand=True,padx=2);self.speed_buttons[speed]=b
        self.link_status=tk.Label(self.controls,bg=T.BG_PANEL,fg=T.WARN_AMBER,font=T.F_SMALL,anchor='w',wraplength=315)
        self.link_status.pack(fill='x',pady=(5,12))
        title(self.controls,'选中相机')
        self.selected_label=tk.Label(self.controls,text=self.selected.upper(),bg=T.BG_PANEL,fg=T.INFO_BLUE,font=T.F_BODY,anchor='w')
        self.selected_label.pack(fill='x',pady=4)
        for buttons in [[('放大 / 四宫格',lambda:self.focus_camera(self.selected) if self.focused is None else self.grid_view()),
                         ('截图',lambda:self.ctls[self.selected].snapshot())],
                        [('单路开始',lambda:self.batch('start',[self.selected])),('单路停止',self.confirm_single_stop)],
                        [('时间对齐…',self.align_dialog),('归档选中…',self.archive_selected)]]:
            row=tk.Frame(self.controls,bg=T.BG_PANEL);row.pack(fill='x',pady=3)
            for text,fn in buttons:self.button(row,text,fn).pack(side='left',fill='x',expand=True,padx=2)
        self.button(self.controls,'测量设备时钟（新赛事自动使用）',self.calibrate).pack(fill='x',pady=(10,3))
        self.clock_label=tk.Label(self.controls,text='历史赛事需校准画面时间',bg=T.BG_PANEL,fg=T.FG_MUTED,font=T.F_SMALL,wraplength=315,justify='left',anchor='w')
        self.clock_label.pack(fill='x',pady=3)
        self.notice=tk.Label(self.controls,text=self.message,bg=T.BG_PANEL,fg=T.WARN_AMBER,
                             font=T.F_SMALL,wraplength=315,justify='left',anchor='nw')
        self.notice.pack(fill='both',expand=True,pady=(12,0))
        title(self.info,'相机状态')
        self.camera_labels={}
        for node in self.nodes:
            label=tk.Label(self.info,bg=T.BG_RAISED,fg=T.FG_TEXT,font=T.F_SMALL,
                            justify='left',anchor='w',padx=8,pady=7)
            label.pack(fill='x',pady=3)
            label.bind('<Button-1>',lambda e,k=node['id']:self.select(k))
            self.camera_labels[node['id']]=label
        title(self.info,'赛事事件')
        event_row=tk.Frame(self.info,bg=T.BG_PANEL);event_row.pack(fill='x',pady=3)
        self.note=tk.Entry(event_row,bg=T.BG_RAISED,fg=T.FG_TEXT,insertbackground=T.FG_TEXT,font=T.F_SMALL,relief='flat')
        self.note.pack(side='left',fill='x',expand=True,ipady=7)
        self.note.bind('<Return>',lambda e:self.add_event())
        self.button(event_row,'＋ 标记',self.add_event).pack(side='right',padx=(4,0))
        self.event_list=tk.Listbox(self.info,bg='#101722',fg=T.FG_TEXT,font=T.F_SMALL,
                                   selectbackground=T.INFO_BLUE_D,relief='flat',highlightthickness=0)
        self.event_list.pack(fill='both',expand=True,pady=6)
        self.event_list.bind('<Double-Button-1>',lambda e:self.jump_event())
        row=tk.Frame(self.info,bg=T.BG_PANEL);row.pack(fill='x')
        for text,fn in [('跳转',self.jump_event),('删除',self.delete_event),('导出',self.export_events)]:
            self.button(row,text,fn).pack(side='left',fill='x',expand=True,padx=2)
        tk.Label(self.info,text='单击选中 · 双击放大\n空格播放/暂停 · ← →逐帧\n拖动底部时间轴联动定位\n关闭窗口不会停止设备录像',
                  bg=T.BG_PANEL,fg=T.FG_MUTED,font=T.F_SMALL,justify='left',anchor='w').pack(fill='x',pady=10)

    def key_action(self,fn):
        if self.root.grab_current() is not None:return
        focus=self.root.focus_get()
        if isinstance(focus,(tk.Entry,ttk.Combobox,tk.Text)):return
        fn();return 'break'

    def layout(self,event=None):
        w,h=self.stage.winfo_width(),self.stage.winfo_height()
        if w<4 or h<4:return
        aspect=2448/2048
        mw=min(h*aspect,w-550)
        mh=mw/aspect
        self.matrix.place(x=0,y=int((h-mh)/2),width=int(mw),height=int(mh))
        self.side.place(x=int(mw)+8,y=0,width=max(540,w-int(mw)-8),height=h)
        keys=list(self.ctls)
        for i,key in enumerate(keys):
            canvas=self.surfaces[key].canvas
            if self.focused is not None:
                if key!=self.focused:canvas.place_forget();continue
                canvas.place(x=0,y=0,relwidth=1,relheight=1)
            else:
                canvas.place(x=int((i%2)*mw/2),y=int((i//2)*mh/2),
                              width=int(mw/2)-1,height=int(mh/2)-1)

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
        self.catalog_busy=True;self.last_catalog=time.monotonic()
        def fetch():
            result={}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures={pool.submit(camera_catalog,n):n['id'] for n in self.nodes}
                for f,key in futures.items():
                    try:result[key]=f.result()
                    except Exception:LOG.exception('Catalog %s unavailable',key)
            return result
        self.job('catalog',fetch)

    def apply_catalog(self,result):
        self.catalog_busy=False
        self.catalogs.update(result)
        if not self.batch_busy:
            for key,sessions in result.items():
                ctl=self.ctls[key]
                if ctl.remote_busy:continue
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
                if s:
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
            self.use_group(self.groups[0],self.group_labels[0])
            self.match_combo.current(0)
            if not any(c.recording for c in self.controllers):
                start=max(t.start for t in self.co.tracks.values())-self.co.origin+.5
                self.co.seek(start,False)
        if not self.groups and not self.co.tracks:self.message='暂无可读录像；可全部开始一场新录制。'
        elif not self.batch_busy:self.message='赛事索引已更新；拖动时间轴选择四路共同回放时刻。'

    def use_group(self,tracks,name):
        self.ring_overlays.clear()
        for ctl in self.controllers:ctl.to_live()
        self.match_id='match_'+uuid.uuid4().hex[:12]
        self.match_name=name;self.events=[]
        self.co.set_tracks(tracks);self.fit_timeline()
        # Restore prior per-match calibration and events when the same four
        # directories are opened again, independently of the combo ordering.
        signature={k:t.directory for k,t in tracks.items()}
        for p in (self.state_root/'matches').glob('*.json'):
            try:
                saved=json.loads(p.read_text())
                if {k:v['directory'] for k,v in saved['tracks'].items()}==signature:
                    self.match_id=saved['id'];self.events=saved.get('events',[])
                    for key,t in tracks.items():
                        v=saved['tracks'][key]
                        for field in ['offset','calibrated','uncertainty','clock_samples']:
                            if field in v:setattr(t,field,v[field])
                    self.co.origin=min(t.start for t in tracks.values());break
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
        save_json(self.state_root/'matches'/(self.match_id+'.json'),
                  dict(id=self.match_id,name=self.match_name,tracks=tracks,events=self.events))

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
        keys=list(keys or self.ctls)
        if any(self.ctls[k].remote_busy for k in keys):
            self.message='所选设备正在执行其他录制操作，请稍候。';return
        if action=='start' and all(self.ctls[k].recording for k in keys):
            self.message='所选相机已在录制，无需重复启动。';return
        self.batch_busy=True;self.batch_action=action;self.batch_pending=set(keys);self.batch_results={}
        self.message='正在%s %d 台相机…'%('启动' if action=='start' else '安全停止',len(keys))
        if action=='start' and len(keys)==4:
            self.ring_overlays.clear()
            self.pending_new=True;self.active_paths={}
            self.match_id='match_'+datetime.now().strftime('%Y%m%d_%H%M%S')
            self.match_name=datetime.now().strftime('%m-%d %H:%M')+' 新赛事'
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
        if self.batch_action=='stop':self.after_stop=True
        if self.pending_new and not self.active_paths:self.pending_new=False
        self.refresh_catalog()

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
        self.clock_label.config(text='软件校时：%d/4 台已测量，往返估计误差 %.1f ms\n历史录像仍需按共同动作确认对齐。'%(len(result),max((v['uncertainty'] for v in result.values()),default=0)*1000))
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
        key=self.selected;track=self.co.tracks.get(key)
        if track is None:return
        if self.ctls[key].recording:
            self.message='请先停止该路录制，再归档完整场次。';return
        self.job('archive-info',lambda:self.ctls[key].archiver.info(track.name),(key,track.name))

    def archive_dialog(self,key,name,info):
        from studio_ui_rk import ArchiveConfirmDialog
        ctl=self.ctls[key]
        dialog=ArchiveConfirmDialog(self.root,ctl,name,info)
        if dialog.result:
            destination,remove=dialog.result
            ctl.start_archive(destination,remove_remote=remove,sess=name,total=info['bytes'])

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
        self.co.live();self.message='四路直播；后台录制与回放操作相互独立。'

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
        for e in self.events:self.event_list.insert('end',tc(e['position'])+'  '+e['note'])

    def jump_event(self):
        sel=self.event_list.curselection()
        if sel:self.co.seek(self.events[sel[0]]['position'],False)

    def delete_event(self):
        sel=self.event_list.curselection()
        if sel:self.events.pop(sel[0]);self.save_match();self.update_events()

    def export_events(self):
        path=filedialog.asksaveasfilename(parent=self.root,title='导出赛事标记',defaultextension='.json',filetypes=[('JSON','*.json')])
        if path:save_json(path,dict(match=self.match_name,events=self.events))

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

    def timeline_motion(self,e):
        self.drag=self.timeline_time(e.x)

    def timeline_release(self,e):
        target=self.timeline_time(e.x);self.drag=None
        if self.co.tracks:self.co.seek(target,self.drag_resume)

    def draw_timeline(self):
        canvas=self.timeline;w=canvas.winfo_width();canvas.delete('all')
        x0=52;x1=w-18;start,span=self.timeline_range()
        if x1<=x0:return
        def x(pos):return x0+(pos-start)/span*(x1-x0)
        for i in range(9):
            p=start+span*i/8;xx=x(p)
            canvas.create_text(xx,9,text=tc(p),fill=T.FG_MUTED,font=T.F_TINY)
            canvas.create_line(xx,19,xx,105,fill=T.BORDER)
        for i,node in enumerate(self.nodes):
            key=node['id'];y=23+i*21
            canvas.create_text(6,y+7,text=key.upper(),fill=T.INFO_BLUE if key==self.selected else T.FG_TEXT,anchor='w',font=T.F_SMALL)
            canvas.create_rectangle(x0,y,x1,y+14,fill=T.BG_RAISED,outline='')
            track=self.co.tracks.get(key)
            if track:
                for a,b in track.intervals():
                    left,right=max(x0,x(a-self.co.origin)),min(x1,x(b-self.co.origin))
                    if right>left:canvas.create_rectangle(left,y,right,y+14,fill='#216e68',outline='')
        for event in self.events:
            xx=x(event['position'])
            if x0<=xx<=x1:canvas.create_polygon(xx-4,20,xx+4,20,xx,26,fill=T.WARN_AMBER)
        pos=self.drag if self.drag is not None else self.co.playhead()
        if self.co.state=='live' and self.drag is None:pos=self.co.duration
        xx=x(pos)
        if x0<=xx<=x1:
            canvas.create_line(xx,16,xx,108,fill=T.REC_RED,width=2)
            canvas.create_polygon(xx-5,16,xx+5,16,xx,23,fill=T.REC_RED)
        self.position_label.config(text='%s / %s%s'%(tc(pos),tc(self.co.duration),'  · 定位中' if self.co.state=='loading' else ''))

    def drain(self):
        while True:
            try:kind,context,result,error=self.results.get_nowait()
            except queue.Empty:break
            if kind=='catalog':
                self.catalog_busy=False
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
            nodes={k:dict(alive=c.feed.alive(),fps=c.live_fps(),image_n=c.feed.image_n,
                         recording=c.recording,mode=c.mode,paused=c.paused,position=c.position_s(),
                         source=self.co.tracks[k].directory if k in self.co.tracks else None,
                         status=self.co.messages.get(k,'')) for k,c in self.ctls.items()}))

    def loop(self):
        if self.closing:return
        now=time.monotonic()
        try:
            self.drain();self.co.tick()
            if self.co.state=='live':
                for ctl in self.controllers:ctl.tick()
            for pad in self.pads:pad.pump()
            if now-self.last_render>.05:
                self.last_render=now
                for surface in self.surfaces.values():
                    if self.focused is None or surface.key==self.focused:surface.draw()
                self.draw_timeline()
                modes={'live':'直播','playing':'联动回放' if self.co.linked else '单路回放','paused':'回放已暂停','loading':'正在共同定位…','buffering':'正在缓冲下一分段…'}
                self.mode_label.config(text=modes[self.co.state],fg=T.LIVE_GREEN if self.co.state=='live' else T.WARN_AMBER)
                self.top_clock.config(text=datetime.now().strftime('%Y-%m-%d  %H:%M:%S'))
                self.selected_label.config(text='%s · %s\n2448 × 2048 原画幅'%(self.selected.upper(),self.ctls[self.selected].args.host))
                count=sum(c.recording for c in self.controllers)
                self.rec_label.config(text='正在录制 %d/4 台%s'%(count,' · 执行中…' if self.batch_busy else ''))
                for b in [self.start_button,self.stop_button]:b.config(state='disabled' if self.batch_busy else 'normal')
                for speed,b in self.speed_buttons.items():b.config(bg=T.INFO_BLUE_D if self.co.speed==speed else T.BG_RAISED)
                calibrated=sum(t.calibrated for t in self.co.tracks.values())
                self.link_status.config(text=('共同指针控制四路' if self.co.linked else '仅控制 '+self.selected.upper())+'\n'+
                    ('时间已软件校准' if calibrated==len(self.co.tracks) and calibrated else '旧录像尚未完成画面对齐'))
                self.notice.config(text=self.message)
                for key,c in self.ctls.items():
                    arch=' · 归档中' if c.archive_busy else ''
                    self.camera_labels[key].config(text='%s  %s%s\n%s · %.1f fps'%(key.upper(),'录制中' if c.recording else '已停止',arch,
                        '在线' if c.feed.alive() else '无直播',c.live_fps()),fg=T.FG_TEXT if c.feed.alive() else T.FG_MUTED)
            if now-self.last_catalog>5 and not self.catalog_busy:self.refresh_catalog()
            if any(c.recording for c in self.controllers) and now-self.last_clock>60 and not self.clock_busy:self.calibrate()
            if now-self.last_health>5:self.last_health=now;self.write_health()
        except Exception:
            LOG.exception('Sports monitor loop failed')
        self.root.after(15,self.loop)

    def close(self):
        if self.closing:return
        self.closing=True
        self.co.shutdown()
        self.save_match()
        for pad in self.pads:pad.stop()
        for ctl in self.controllers:ctl.shutdown()
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.root.destroy()
