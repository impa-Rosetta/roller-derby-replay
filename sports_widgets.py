"""Native Tk rounded cards, vector icons and accessible soft buttons."""
import tkinter as tk
from tkinter import font as tkfont
import sports_theme as T


def rounded(canvas,x0,y0,x1,y1,r,**kwargs):
    r=max(0,min(r,(x1-x0)/2,(y1-y0)/2))
    return canvas.create_polygon(x0+r,y0,x1-r,y0,x1,y0,x1,y0+r,
        x1,y1-r,x1,y1,x1-r,y1,x0+r,y1,x0,y1,x0,y1-r,
        x0,y0+r,x0,y0,smooth=True,splinesteps=24,**kwargs)


class RoundedCard(tk.Canvas):
    def __init__(self,parent,fill=T.BG_PANEL,padding=14,radius=16,expand=False,shadow=True):
        super().__init__(parent,bg=parent.cget('bg'),highlightthickness=0,bd=0,width=100,height=1)
        self.fill=fill;self.padding=padding;self.radius=radius;self.expand_body=expand;self.shadow=shadow
        self.body=tk.Frame(self,bg=fill,bd=0)
        self.window=self.create_window(padding,padding,window=self.body,anchor='nw')
        self.bind('<Configure>',self._layout)
        self.body.bind('<Configure>',self._content_size)

    def _content_size(self,event=None):
        if not self.expand_body:
            height=self.body.winfo_reqheight()+self.padding*2+4
            if int(self.cget('height'))!=height:self.configure(height=height)

    def _layout(self,event=None):
        w,h=self.winfo_width(),self.winfo_height()
        p=self.padding
        self.coords(self.window,p,p)
        self.itemconfigure(self.window,width=max(1,w-2*p),height=max(1,h-2*p-3) if self.expand_body else 0)
        self.delete('decoration')
        if self.shadow:
            rounded(self,2,4,w-1,h-1,self.radius,fill='#dee7f5',outline='',tags='decoration')
        rounded(self,1,1,w-3,h-4,self.radius,fill=self.fill,outline='',tags='decoration')
        self.tag_lower('decoration')
        self._content_size()


class SoftButton(tk.Canvas):
    def __init__(self,parent,text,command,color=None,font=T.F_BODY,height=44,symbol=None):
        self.text=text;self.command=command;self.color=color or T.BG_RAISED
        self.symbol=symbol
        self.enabled=True;self.hover=False;self.pressed=False
        self.font=tkfont.Font(root=parent,font=font)
        width=self.font.measure(text)+22
        super().__init__(parent,width=width,height=height,bg=parent.cget('bg'),
                         highlightthickness=0,bd=0,takefocus=1,cursor='hand2')
        self.bind('<Configure>',lambda e:self._draw())
        self.bind('<Enter>',lambda e:self._hover(True))
        self.bind('<Leave>',lambda e:self._hover(False))
        self.bind('<ButtonPress-1>',self._press)
        self.bind('<ButtonRelease-1>',self._release)
        self.bind('<FocusIn>',lambda e:self._draw())
        self.bind('<FocusOut>',lambda e:self._draw())
        self.bind('<Return>',self._key)
        self.bind('<space>',self._key)
        self.after_idle(self._draw)

    def config(self,cnf=None,**kwargs):
        changes=dict(cnf or {});changes.update(kwargs);redraw=False
        if 'state' in changes:
            value=changes.pop('state')!='disabled'
            redraw|=value!=self.enabled;self.enabled=value
        if 'bg' in changes or 'background' in changes:
            value=changes.pop('bg',changes.pop('background',self.color))
            redraw|=value!=self.color;self.color=value
        if 'text' in changes:
            value=changes.pop('text');redraw|=value!=self.text;self.text=value
        if 'symbol' in changes:
            value=changes.pop('symbol');redraw|=value!=self.symbol;self.symbol=value
        if 'command' in changes:self.command=changes.pop('command')
        if changes:super().configure(**changes)
        if redraw:self._draw()

    configure=config

    def cget(self,key):
        if key=='state':return 'normal' if self.enabled else 'disabled'
        if key=='text':return self.text
        if key=='symbol':return self.symbol
        return super().cget(key)

    def _hover(self,value):
        self.hover=value;self.pressed=False;self._draw()

    def _press(self,event):
        if self.enabled:self.focus_set();self.pressed=True;self._draw()

    def _release(self,event):
        pressed=self.pressed;self.pressed=False;self._draw()
        if pressed and 0<=event.x<self.winfo_width() and 0<=event.y<self.winfo_height():self.invoke()

    def _key(self,event):
        self.invoke();return 'break'

    def invoke(self):
        if self.enabled and self.command:return self.command()

    def _draw(self):
        if not self.winfo_exists():return
        w,h=self.winfo_width(),self.winfo_height()
        if w<3:return
        self.delete('all')
        primary=self.color in (T.INFO_BLUE_D,T.REC_RED_D,T.LIVE_GREEN_D)
        color=self.color
        text='#ffffff' if primary else T.FG_TEXT
        outline=''
        if not self.enabled:color='#f1f4f9';text='#9aa8bc'
        elif self.hover or self.pressed:
            if primary:
                rgb=[int(color[i:i+2],16) for i in (1,3,5)]
                color='#'+''.join('%02x'%int(c*.91) for c in rgb)
            else:color=T.BG_HOVER;outline='#c5d8fc'
        if self.focus_get()==self:outline=T.INFO_BLUE
        rounded(self,1,1,w-1,h-1,9,fill=color,outline=outline,width=1)
        if self.symbol=='play':
            self.create_polygon(w/2-5,h/2-8,w/2-5,h/2+8,w/2+8,h/2,fill=text,outline='',tags='transport-icon')
        elif self.symbol=='pause':
            for x in [w/2-7,w/2+2]:self.create_rectangle(x,h/2-8,x+5,h/2+8,fill=text,outline='',tags='transport-icon')
        else:self.create_text(w/2,h/2,text=self.text,fill=text,font=self.font)


def transport_state(state,resume_after_load=True):
    moving=state in ('live','playing') or (state in ('loading','buffering') and resume_after_load)
    return ('pause','暂停') if moving else ('play','播放')


class IconBadge(tk.Canvas):
    def __init__(self,parent,kind='camera',color=T.INFO_BLUE,size=32,solid=False):
        super().__init__(parent,width=size,height=size,bg=parent.cget('bg'),highlightthickness=0)
        fill=color if solid else T.INFO_SOFT
        line='#ffffff' if solid else color
        rounded(self,1,1,size-1,size-1,9,fill=fill,outline='')
        s=size/32
        def rectangle(a,b,c,d,**kw):self.create_rectangle(a*s,b*s,c*s,d*s,**kw)
        if kind=='play':
            self.create_polygon(13*s,9*s,24*s,16*s,13*s,23*s,fill=line,outline='')
        elif kind=='clock':
            self.create_oval(8*s,8*s,24*s,24*s,outline=line,width=2)
            self.create_line(16*s,11*s,16*s,17*s,21*s,19*s,fill=line,width=2)
        elif kind=='mark':
            rectangle(10,8,22,25,outline=line,width=2)
            self.create_line(13*s,13*s,19*s,13*s,fill=line,width=2)
            self.create_line(13*s,18*s,19*s,18*s,fill=line,width=2)
        else:
            rounded(self,7*s,11*s,25*s,23*s,3*s,outline=line,width=2,fill='')
            rectangle(11,8,19,11,fill=line,outline='')
            self.create_oval(13*s,13*s,20*s,20*s,outline=line,width=2)


def section_heading(parent,text,kind='camera',subtitle=None,color=T.INFO_BLUE):
    row=tk.Frame(parent,bg=parent.cget('bg'));row.pack(fill='x',pady=(0,9))
    IconBadge(row,kind,color=color).pack(side='left',padx=(0,9))
    labels=tk.Frame(row,bg=row.cget('bg'));labels.pack(side='left',fill='x',expand=True)
    tk.Label(labels,text=text,bg=row.cget('bg'),fg=T.FG_TEXT,font=T.F_TITLE,anchor='w').pack(fill='x')
    if subtitle:tk.Label(labels,text=subtitle,bg=row.cget('bg'),fg=T.FG_MUTED,font=T.F_TINY,anchor='w').pack(fill='x')
    return row
