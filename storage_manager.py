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
        self.snapshot = {};self.rows = {};self.last_scan = 0
        self.window = tk.Toplevel(app.root)
        self.window.title('录像与存储管理 · RK3588 体育赛事回放平台')
        self.window.configure(bg=T.BG_APP);self.window.geometry('1320x820');self.window.minsize(1050,650)
        self.window.transient(app.root);self.window.protocol('WM_DELETE_WINDOW', self.close)
        header = tk.Frame(self.window, bg=T.BG_APP);header.pack(fill='x',padx=18,pady=(16,8))
        tk.Label(header,text='录像与存储管理',font=T.F_BRAND,bg=T.BG_APP,fg=T.FG_TEXT).pack(side='left')
        self.refresh_button = SoftButton(header,'刷新空间与录像',self.refresh)
        self.refresh_button.pack(side='right')
        tk.Label(self.window,text='录像按设备和副本位置列出；归档只复制，不自动删除 RK 原件。',
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
        tk.Label(filters,text='位置',bg=T.BG_APP,fg=T.FG_TEXT,font=T.F_BODY).pack(side='left')
        self.location = ttk.Combobox(filters,state='readonly',width=13,values=['全部位置','RK 原件','DGX 归档'])
        self.location.current(0);self.location.pack(side='left',padx=8)
        self.location.bind('<<ComboboxSelected>>',lambda e:self.render_rows())
        self.only_archived = tk.BooleanVar(value=False)
        tk.Checkbutton(filters,text='仅看已有校验归档的 RK 原件',variable=self.only_archived,
                       command=self.render_rows,bg=T.BG_APP,font=T.F_SMALL).pack(side='left',padx=8)
        self.search = tk.StringVar()
        tk.Label(filters,text='搜索',bg=T.BG_APP,fg=T.FG_TEXT,font=T.F_BODY).pack(side='left',padx=(14,5))
        ttk.Entry(filters,textvariable=self.search,width=27).pack(side='left')
        self.search.trace_add('write',lambda *a:self.render_rows())
        table = tk.Frame(self.window,bg=T.BG_PANEL);table.pack(fill='both',expand=True,padx=18)
        style = ttk.Style(self.window)
        style.configure('Storage.Treeview',rowheight=34,font=T.F_SMALL,background=T.BG_PANEL,
                        fieldbackground=T.BG_PANEL,foreground=T.FG_TEXT,borderwidth=0)
        style.configure('Storage.Treeview.Heading',font=T.F_SMALL,background=T.INFO_SOFT,foreground=T.FG_TEXT)
        style.map('Storage.Treeview',background=[('selected',T.INFO_BLUE)],foreground=[('selected','#ffffff')])
        columns = [('time','录制时间',155),('node','机位',65),('location','存放位置',95),
                   ('duration','时长',90),('size','占用空间',100),('status','状态 / 副本',260),('name','录像目录',230)]
        self.tree = ttk.Treeview(table,columns=[c[0] for c in columns],show='headings',
                                 selectmode='extended',style='Storage.Treeview')
        for key,text,width in columns:
            self.tree.heading(key,text=text);self.tree.column(key,width=width,minwidth=55,stretch=key in ('status','name'))
        y = ttk.Scrollbar(table,orient='vertical',command=self.tree.yview)
        x = ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        y.pack(side='right',fill='y');x.pack(side='bottom',fill='x');self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',lambda e:self.selection_status())
        self.tree.bind('<Double-Button-1>',lambda e:self.preview())
        self.tree.bind('<Control-a>',lambda e:(self.tree.selection_set(*self.tree.get_children()),'break')[-1])
        actions = tk.Frame(self.window,bg=T.BG_APP);actions.pack(fill='x',padx=16,pady=10)
        self.action_buttons = []
        for text,fn,color in [('回放选中',self.preview,None),('选中同场录像',self.select_match,None),
                              ('归档至 DGX',self.archive,T.INFO_BLUE_D),
                              ('删除 RK 原件',lambda:self.delete('rk'),T.REC_RED_D),
                              ('删除 DGX 归档',lambda:self.delete('dgx'),None)]:
            b = SoftButton(actions,text,fn,color=color);b.pack(side='left',padx=4);self.action_buttons.append(b)
        self.selection_label = tk.Label(self.window,bg=T.BG_APP,fg=T.FG_MUTED,font=T.F_SMALL,anchor='w')
        self.selection_label.pack(fill='x',padx=20)
        self.status = tk.Label(self.window,text='正在读取设备…',bg=T.INFO_SOFT,fg=T.FG_TEXT,
                               font=T.F_SMALL,anchor='w',justify='left',wraplength=1230,padx=10,pady=8)
        self.status.pack(fill='x',padx=18,pady=(8,14))
        self.after_id = None
        self.refresh();self.after_id = self.window.after(100,self.pump)

    def selected(self):
        return [self.rows[i] for i in self.tree.selection() if i in self.rows]

    def selection_status(self):
        rows = self.selected()
        self.selection_label.config(text='已选择 %d 份录像 · %.2f GB  |  Ctrl 多选 / Shift 连选；双击回放'%
                                    (len(rows),sum(r['bytes'] for r in rows)/1e9))

    def refresh(self):
        if self.busy or self.scanning:return
        self.scanning = True;self.last_scan = time.monotonic()
        self.status.config(text='正在读取五台设备的空间与录像列表…')
        future = self.pool.submit(self.service.snapshot)
        future.add_done_callback(lambda f:self.events.put(('scan',f)))

    def render_rows(self):
        selection = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children());self.rows = {}
        query = self.search.get().strip().lower();where = self.location.current()
        for row in self.snapshot.get('rows',[]):
            if where == 1 and row['location'] != 'rk':continue
            if where == 2 and row['location'] != 'dgx':continue
            if self.only_archived.get() and not (row['location'] == 'rk' and row['archive_verified']):continue
            if query and query not in (row['name']+' '+row['node']).lower():continue
            location = row['node'].upper()+' 原件' if row['location'] == 'rk' else 'DGX 归档'
            if row['location'] == 'rk':
                state = 'DGX 校验归档已存在' if row['archive_verified'] else ('DGX 有副本 · 待校验' if row['other_copy'] else '仅 RK 有原件')
                if row['busy']:state = '节点录制中 · 暂不可操作'
            else:
                state = ('归档已校验' if row['verified'] else '归档待校验')+(' · RK 也有副本' if row['other_copy'] else ' · RK 无可用副本或离线')
            stamp = datetime.fromtimestamp(row['start']).strftime('%Y-%m-%d %H:%M:%S') if row['start'] else row['name'][7:]
            self.tree.insert('', 'end',iid=row['id'],values=(stamp,row['node'].upper(),location,
                             duration(row['seconds']),'%.2f GB'%(row['bytes']/1e9),state,row['name']))
            self.rows[row['id']] = row
        kept = selection.intersection(self.rows)
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
        self.status.config(text=('已读取 %d 份录像副本；RK 预计时长已扣除 10 GB 停录预留。'%len(snapshot['rows']))+
                           ('\n部分读取失败：'+'；'.join(k+': '+v for k,v in errors.items()) if errors else ''))

    def select_match(self):
        rows = self.selected()
        if not rows:return
        anchor = rows[0]
        matches = []
        for node in self.app.ctls:
            candidates = [r for r in self.rows.values() if r['node'] == node and
                          r['location'] == anchor['location'] and r['start'] and anchor['start'] and
                          abs(r['start']-anchor['start']) < 30]
            if candidates:matches.append(min(candidates,key=lambda r:abs(r['start']-anchor['start']))['id'])
        self.tree.selection_set(*matches)
        self.status.config(text='已按起始时间相近选择同场候选录像；操作前请核对场次与时长。')

    def preview(self):
        if self.busy:return
        tracks = {}
        for row in self.selected():
            if row['segments']:
                tracks[row['node']] = Track(row['node'],row['name'],row['directory'],row['segments'])
        if not tracks:
            self.status.config(text='请选择至少一份包含有效分段的录像。');return
        for key,track in tracks.items():
            if track.directory.startswith(self.service.archive_roots[key]+'/'):
                roots = self.app.ctls[key].loader.local_dirs
                if self.service.archive_roots[key] not in roots:roots.append(self.service.archive_roots[key])
        self.app.use_group(tracks,'录像管理 · '+next(iter(tracks.values())).name)
        start = max(t.start for t in tracks.values())-self.app.co.origin+.1
        self.app.co.seek(start,False)
        self.app.match_combo.set(self.app.match_name)
        self.window.lower();self.app.root.lift()

    def preflight(self, rows):
        if self.busy or self.scanning:return False
        if self.app.batch_busy or any(c.remote_busy or c.archive_busy for c in self.app.controllers):
            self.status.config(text='录制启停或归档任务尚未完成，请稍后。');return False
        if not rows:
            self.status.config(text='请先选择需要操作的录像。');return False
        return True

    def archive(self):
        rows = [r for r in self.selected() if r['location'] == 'rk']
        if not self.preflight(rows):return
        if any(r['busy'] for r in rows):
            self.status.config(text='所选节点正在录制，请先停止录制后归档。');return
        text = self.confirm_list(rows)
        if messagebox.askyesno('归档至 DGX',text+'\n\n将复制并校验以上录像，保留 RK 原件。',parent=self.window):
            self.start_operation('archive',rows)

    def delete(self, location):
        rows = [r for r in self.selected() if r['location'] == location]
        if not self.preflight(rows):return
        if location == 'rk' and any(r['busy'] for r in rows):
            self.status.config(text='所选节点正在录制，禁止删除。');return
        place = 'RK 原件' if location == 'rk' else 'DGX 归档'
        text = self.confirm_list(rows)
        uncertain = sum(not (r['archive_verified'] if location == 'rk' else r['other_copy']) for r in rows)
        note = '\n其中 %d 份没有确认可用的另一处副本。'%uncertain if uncertain else ''
        if messagebox.askyesno('永久删除'+place,
                              text+'\n\n仅删除以上'+place+'，共 %.2f GB。'%(sum(r['bytes'] for r in rows)/1e9)+
                              note+'\n删除后无法撤销，是否继续？',icon='warning',parent=self.window):
            self.start_operation('delete',rows)

    @staticmethod
    def confirm_list(rows):
        text = '\n'.join(r['node'].upper()+' / '+r['name'] for r in rows[:10])
        if len(rows)>10:text+='\n…另 %d 份，共 %d 份（见列表中选中项）'%(len(rows)-10,len(rows))
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
        future.add_done_callback(lambda f:self.events.put(('operation',f)))

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
        self.pool.shutdown(wait=False,cancel_futures=True);self.window.destroy()
