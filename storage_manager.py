"""Blue/white recording manager for the existing sports desk."""
import concurrent.futures
from datetime import datetime
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox

import sports_theme as T
from sports_widgets import RoundedCard, SoftButton
from sports_replay import Track
from storage_service import StorageService


def duration(seconds):
    seconds = max(0, int(seconds))
    return '%02d:%02d:%02d' % (seconds//3600, seconds//60%60, seconds%60)


class StorageManager:
    def __init__(self, app):
        self.app = app
        self.service = StorageService(app.nodes, {k: c.archive_dir for k, c in app.ctls.items()}, app.state_root)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.events = queue.Queue()
        self.busy = False;self.scanning = False;self.closed = False
        self.snapshot = {};self.rows = {};self.matches = {};self.last_scan = 0
        self.window = tk.Toplevel(app.root)
        self.window.title('录像与存储管理 · RK3588 体育赛事回放平台')
        self.window.configure(bg=T.BG_APP);self.window.geometry('1320x820');self.window.minsize(1050,650)
        self.window.transient(app.root);self.window.protocol('WM_DELETE_WINDOW', self.close)
        header = tk.Frame(self.window, bg=T.BG_APP);header.pack(fill='x',padx=18,pady=(16,8))
        tk.Label(header,text='录像与存储管理',font=T.F_BRAND,bg=T.BG_APP,fg=T.FG_TEXT).pack(side='left')
        self.refresh_button = SoftButton(header,'刷新空间与录像',self.refresh)
        self.refresh_button.pack(side='right')
        tk.Label(self.window,text='每场比赛包含四个机位；展开查看详情，回放、归档和删除均以比赛为单位。',
                 bg=T.BG_APP,fg=T.FG_MUTED,font=T.F_BODY).pack(anchor='w',padx=20)
        disk_row = tk.Frame(self.window,bg=T.BG_APP);disk_row.pack(fill='x',padx=14,pady=12)
        self.disk_labels = {}
        for i,key in enumerate(['dgx']+list(app.ctls)):
            disk_row.columnconfigure(i,weight=1)
            card = RoundedCard(disk_row,padding=12);card.grid(row=0,column=i,sticky='nsew',padx=4)
            label = tk.Label(card.body,text=key.upper()+'\n读取中…',bg=T.BG_PANEL,fg=T.INFO_BLUE,
                             font=T.F_SMALL,justify='left',anchor='w')
            label.pack(fill='x');self.disk_labels[key] = label
        filters = tk.Frame(self.window,bg=T.BG_APP);filters.pack(fill='x',padx=18,pady=(0,8))
        tk.Label(filters,text='归档状态',bg=T.BG_APP,fg=T.FG_TEXT,font=T.F_BODY).pack(side='left')
        self.location = ttk.Combobox(filters,state='readonly',width=16,values=['全部比赛','未完整归档','DGX 有归档'])
        self.location.current(0);self.location.pack(side='left',padx=8)
        self.location.bind('<<ComboboxSelected>>',lambda e:self.render_rows())
        self.search = tk.StringVar(master=self.window)
        tk.Label(filters,text='搜索',bg=T.BG_APP,fg=T.FG_TEXT,font=T.F_BODY).pack(side='left',padx=(14,5))
        ttk.Entry(filters,textvariable=self.search,width=27).pack(side='left')
        self.search_trace = self.search.trace_add('write',lambda *a:self.render_rows())
        table = tk.Frame(self.window,bg=T.BG_PANEL);table.pack(fill='both',expand=True,padx=18)
        style = ttk.Style(self.window)
        style.configure('Storage.Treeview',rowheight=34,font=T.F_SMALL,background=T.BG_PANEL,
                        fieldbackground=T.BG_PANEL,foreground=T.FG_TEXT,borderwidth=0)
        style.configure('Storage.Treeview.Heading',font=T.F_SMALL,background=T.INFO_SOFT,foreground=T.FG_TEXT)
        style.map('Storage.Treeview',background=[('selected',T.INFO_BLUE)],foreground=[('selected','#ffffff')])
        columns = [('time','录制时间',155),('duration','比赛时长',95),('rk','RK 原件占用',115),
                   ('dgx','DGX 归档占用',115),('status','完整性 / 归档状态',240)]
        self.tree = ttk.Treeview(table,columns=[c[0] for c in columns],show='tree headings',
                                 selectmode='extended',style='Storage.Treeview')
        self.tree.heading('#0',text='比赛 / 四机位详情');self.tree.column('#0',width=390,minwidth=230)
        for key,text,width in columns:
            self.tree.heading(key,text=text);self.tree.column(key,width=width,minwidth=55,stretch=key=='status')
        y = ttk.Scrollbar(table,orient='vertical',command=self.tree.yview)
        x = ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        y.pack(side='right',fill='y');x.pack(side='bottom',fill='x');self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',lambda e:self.selection_status())
        self.tree.bind('<Double-Button-1>',lambda e:self.preview())
        self.tree.bind('<Control-a>',lambda e:(self.tree.selection_set(*self.tree.get_children()),'break')[-1])
        actions = tk.Frame(self.window,bg=T.BG_APP);actions.pack(fill='x',padx=16,pady=10)
        self.action_buttons = []
        for text,fn,color in [('回放整场比赛',self.preview,None),('展开 / 收起四机位',self.select_match,None),
                              ('归档整场至 DGX',self.archive,T.INFO_BLUE_D),
                              ('清理本场 RK 副本',lambda:self.delete('rk'),None),
                              ('删除整场比赛',lambda:self.delete('all'),T.REC_RED_D)]:
            b = SoftButton(actions,text,fn,color=color);b.pack(side='left',padx=4);self.action_buttons.append(b)
        self.selection_label = tk.Label(self.window,bg=T.BG_APP,fg=T.FG_MUTED,font=T.F_SMALL,anchor='w')
        self.selection_label.pack(fill='x',padx=20)
        self.status = tk.Label(self.window,text='正在读取设备…',bg=T.INFO_SOFT,fg=T.FG_TEXT,
                               font=T.F_SMALL,anchor='w',justify='left',wraplength=1230,padx=10,pady=8)
        self.status.pack(fill='x',padx=18,pady=(8,14))
        self.after_id = None
        self.refresh();self.after_id = self.window.after(100,self.pump)

    def selected(self):
        return [r for match in self.selected_matches() for r in match['members']]

    def selected_matches(self):
        ids = {self.tree.parent(i) or i for i in self.tree.selection()}
        return [match for key,match in self.matches.items() if key in ids]

    def selection_status(self):
        rows = self.selected()
        self.selection_label.config(text='已选择 %d 场比赛 · %d 个机位 · %.2f GB  |  Ctrl 多选比赛；双击整场回放'%
                                    (len(self.selected_matches()),len({(r['node'],r['name']) for r in rows}),
                                     sum(r['bytes'] for r in rows)/1e9))

    def refresh(self):
        if self.busy or self.scanning:return
        self.scanning = True;self.last_scan = time.monotonic()
        self.status.config(text='正在读取五台设备的空间与录像列表…')
        future = self.pool.submit(self.service.snapshot)
        events = self.events
        future.add_done_callback(lambda f:events.put(('scan',f)))

    def render_rows(self):
        selection = {self.tree.parent(i) or i for i in self.tree.selection()}
        expanded = {i for i in self.tree.get_children() if self.tree.item(i,'open')}
        self.tree.delete(*self.tree.get_children());self.rows = {};self.matches = {}
        query = self.search.get().strip().lower();where = self.location.current()
        for match in self.snapshot.get('matches',[]):
            if where == 1 and match['verified_count']==match['expected']:continue
            if where == 2 and not match['dgx_count']:continue
            if query and query not in (match['name']+' '+' '.join(r['name']+' '+r['node'] for r in match['members'])).lower():continue
            key = match['id'];self.matches[key] = match
            stamp = datetime.fromtimestamp(match['start']).strftime('%Y-%m-%d %H:%M:%S') if match['start'] else '时间未知'
            state = '%d/%d 机位 · 已校验归档 %d/%d'%(match['camera_count'],match['expected'],match['verified_count'],match['expected'])
            if match['busy']:state += ' · 节点录制中'
            self.tree.insert('', 'end',iid=key,text=match['name'],open=key in expanded,
                             values=(stamp,duration(match['seconds']),'%.2f GB'%(match['rk_bytes']/1e9),
                                     '%.2f GB'%(match['dgx_bytes']/1e9),state))
            for node in self.app.ctls:
                copies = [r for r in match['members'] if r['node']==node]
                rk = next((r for r in copies if r['location']=='rk'),None)
                dgx = next((r for r in copies if r['location']=='dgx'),None)
                detail = ('RK 原件' if rk else '')+(' + ' if rk and dgx else '')+('DGX 归档' if dgx else '')
                if not detail:detail = '无可用录像 / 节点离线'
                self.tree.insert(key,'end',iid=key+'|'+node,text='  '+node.upper(),
                                 values=('',duration(max((r['seconds'] for r in copies),default=0)),
                                         '%.2f GB'%(rk['bytes']/1e9) if rk else '—',
                                         '%.2f GB'%(dgx['bytes']/1e9) if dgx else '—',detail))
            self.rows.update({r['id']:r for r in match['members']})
        kept = selection.intersection(self.matches)
        if kept:self.tree.selection_set(*kept)
        self.selection_status()

    def show_snapshot(self, snapshot):
        self.snapshot = snapshot
        for key,label in self.disk_labels.items():
            disk = snapshot['disks'].get(key)
            if disk:
                budget = max(0,disk['available']-(10e9 if key != 'dgx' else 0))
                hours = budget/(54e9 if key == 'dgx' else 13.5e9)
                label.config(text='%s\n可用 %.1f / %.1f GB\n%s约 %s'%
                             (key.upper(),disk['available']/1e9,disk['total']/1e9,
                              '四路容量 ' if key == 'dgx' else '还能录 ',duration(hours*3600)))
            else:label.config(text=key.upper()+'\n设备离线\n空间暂不可读')
        self.render_rows()
        errors = snapshot['errors']
        self.status.config(text=('共 %d 场比赛；点击比赛即可操作其全部机位，展开可查看存放位置。'%len(snapshot.get('matches',[])))+
                           ('\n部分读取失败：'+'；'.join(k+': '+v for k,v in errors.items()) if errors else ''))

    def select_match(self):
        for match in self.selected_matches():
            key=match['id'];self.tree.item(key,open=not self.tree.item(key,'open'))

    def preview(self):
        if self.busy:return
        matches = self.selected_matches()
        if len(matches)!=1:
            self.status.config(text='请选择一场比赛进行四路回放。');return
        match = matches[0]
        tracks = {}
        preferred = {}
        for row in sorted(match['members'],key=lambda r:r['location']=='rk'):
            preferred[row['node']] = row
        for row in preferred.values():
            if row['segments']:
                track = Track(row['node'],row['name'],row['directory'],row['segments'])
                for field in ['offset','calibrated','uncertainty','clock_samples']:
                    if field in match['track_settings'].get(row['node'],{}):
                        setattr(track,field,match['track_settings'][row['node']][field])
                tracks[row['node']] = track
        if not tracks:
            self.status.config(text='本场比赛暂时没有可读取的视频分段。');return
        for key,track in tracks.items():
            if track.directory.startswith(self.service.archive_roots[key]+'/'):
                roots = self.app.ctls[key].loader.local_dirs
                if self.service.archive_roots[key] not in roots:roots.append(self.service.archive_roots[key])
        self.app.use_group(tracks,match['name'])
        start = max(t.start for t in tracks.values())-self.app.co.origin+.1
        self.app.co.seek(start,False)
        self.app.match_combo.set(self.app.match_name)
        self.window.lower();self.app.root.lift()

    def preflight(self, rows):
        if self.busy or self.scanning:return False
        if self.app.batch_busy or any(c.remote_busy or c.archive_busy for c in self.app.controllers):
            self.status.config(text='录制启停或归档任务尚未完成，请稍后。');return False
        if not rows:
            self.status.config(text='请先选择需要操作的比赛。');return False
        return True

    def archive(self):
        rows = [r for r in self.selected() if r['location'] == 'rk']
        if not rows and self.selected_matches():
            self.status.config(text='所选比赛没有 RK 原件，无需再次归档。');return
        if not self.preflight(rows):return
        if any(r['busy'] for r in rows):
            self.status.config(text='所选节点正在录制，请先停止录制后归档。');return
        text = self.confirm_list(rows)
        if messagebox.askyesno('归档整场比赛',text+'\n\n将复制并校验所选比赛的全部 RK 机位录像，保留 RK 原件。',parent=self.window):
            self.start_operation('archive',rows)

    def delete(self, location):
        rows = [r for r in self.selected() if location=='all' or r['location'] == location]
        if not self.preflight(rows):return
        if location=='all':
            owned={r['node'] for r in rows}
            for match in self.selected_matches():owned.update(match['track_settings'])
            if any(node+'|rk' in self.snapshot.get('errors',{}) or node+'|dgx' in self.snapshot.get('errors',{}) for node in owned):
                self.status.config(text='本场有节点或归档目录读取失败，请恢复连接后再删除整场，避免遗漏副本。');return
        if any(r['busy'] for r in rows if r['location']=='rk'):
            self.status.config(text='所选节点正在录制，禁止删除。');return
        place = '比赛的 RK 原件' if location == 'rk' else '整场比赛（RK 原件和 DGX 归档）'
        text = self.confirm_list(rows)
        uncertain = sum(not r['archive_verified'] for r in rows if r['location']=='rk')
        note = ('\n将删除本场全部机位、全部存储位置的视频。' if location=='all' else
                '\n其中 %d 个机位没有确认完整的 DGX 校验归档。'%uncertain if uncertain else '')
        if messagebox.askyesno('永久删除'+place,
                              text+'\n\n仅删除以上'+place+'，共 %.2f GB。'%(sum(r['bytes'] for r in rows)/1e9)+
                              note+'\n删除后无法撤销，是否继续？',icon='warning',parent=self.window):
            self.start_operation('delete',rows)

    def confirm_list(self,rows):
        matches = self.selected_matches()
        text = '\n'.join(m['name']+'（%d/%d 机位）'%(m['camera_count'],m['expected']) for m in matches[:8])
        if len(matches)>8:text+='\n…共 %d 场比赛'%len(matches)
        text+='\n共 %d 个机位、%d 份存储副本'%(len({(r['node'],r['name']) for r in rows}),len(rows))
        return text

    def start_operation(self, action, rows):
        self.busy = True
        for b in self.action_buttons+[self.refresh_button]:b.config(state='disabled')
        if action == 'delete':self.app.storage_before_delete(rows)
        def run():
            results = []
            for i,row in enumerate(rows):
                prefix = '[%d/%d] %s %s'%(i+1,len(rows),row['node'].upper(),row['name'])
                self.events.put(('progress',prefix))
                try:
                    if action == 'archive':
                        value = self.service.archive(row,lambda s:self.events.put(('progress',prefix+' · '+s)))
                    else:value = self.service.delete(row)
                    results.append((row,True,value))
                except Exception as exc:
                    if action == 'archive':self.service.audit(action,row,'FAIL',str(exc))
                    results.append((row,False,str(exc)))
            return action, results, self.service.snapshot()
        future = self.pool.submit(run)
        events = self.events
        future.add_done_callback(lambda f:events.put(('operation',f)))

    def pump(self):
        if self.closed:return
        if self.after_id:
            try:self.window.after_cancel(self.after_id)
            except tk.TclError:pass
            self.after_id=None
        while True:
            try:kind,value = self.events.get_nowait()
            except queue.Empty:break
            if kind == 'progress':self.status.config(text=value);continue
            try:
                result = value.result()
                if kind == 'scan':
                    self.scanning = False;self.show_snapshot(result)
                else:
                    action, outcomes, snapshot = result
                    self.busy = False
                    for b in self.action_buttons+[self.refresh_button]:b.config(state='normal')
                    self.show_snapshot(snapshot)
                    self.app.storage_after_operation(action,outcomes,snapshot)
                    ok = sum(success for row,success,detail in outcomes)
                    failures = [row['node'].upper()+'/'+row['name']+': '+str(detail)
                                for row,success,detail in outcomes if not success]
                    self.status.config(text='%s完成：成功 %d，失败 %d。'%
                                       ('归档校验' if action == 'archive' else '删除',ok,len(outcomes)-ok)+
                                       ('\n'+'；'.join(failures) if failures else ''))
            except Exception as exc:
                self.scanning = False;self.busy = False
                for b in self.action_buttons+[self.refresh_button]:b.config(state='normal')
                self.status.config(text='操作失败：'+str(exc))
        if not self.busy and time.monotonic()-self.last_scan > 30:self.refresh()
        self.after_id=self.window.after(100,self.pump)

    def close(self):
        if self.busy:
            self.status.config(text='后台操作进行中，请等待完成后关闭。');return
        self.closed = True
        if self.after_id:self.window.after_cancel(self.after_id)
        self.search.trace_remove('write',self.search_trace)
        self.pool.shutdown(wait=False,cancel_futures=True);self.window.destroy()
