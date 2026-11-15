#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缩略直播查看器：只看 Jetson 推过来的低码率画面。

用途与监看台不同 —— 监看台管录制/归档/标记，这里**只负责看**：
  - 启动快，连上就出画面，不 ssh、不管录制
  - 支持多路（四机位时加 --host / --port 即可）同屏
  - 实时显示每路的码率、帧率、端到端延迟、连接状态

用法:
  /home/ifit/xjh/run_thumb.sh                       # 自动探测 Jetson
  python3 thumb_view.py --host 192.168.123.2
  python3 thumb_view.py --host 192.168.123.2:9102 --host 192.168.123.3:9102
"""
import argparse
import time

import cv2

import studio_services as svc

# 延迟导入：没有 DISPLAY 时不该在这一步就崩
tk = None


class Tile:
    """一路画面的状态与渲染。"""

    def __init__(self, name, host, port, width):
        self.name = name
        self.host = host
        self.port = port
        self.feed = svc.LiveFeed(host, port, width, want=True)
        self._seen_n = 0
        # 出帧率：直接数 LiveFeed.image_n 的增量。不能自己记账——画面
        # 被 UI 取走的节奏跟解码节奏不同，记出来是 UI 刷新率不是帧率。
        self._fps_t0 = time.monotonic()
        self._fps_n0 = 0
        self.decode_fps = 0.0

    def start(self):
        self.feed.start()

    def stop(self):
        self.feed.stop()

    def tick_stats(self):
        now = time.monotonic()
        dt = now - self._fps_t0
        if dt >= 1.0:
            n = self.feed.image_n
            self.decode_fps = (n - self._fps_n0) / dt
            self._fps_t0 = now
            self._fps_n0 = n

    def status_line(self):
        f = self.feed
        if f.image is None:
            return f.status
        n = f.image_n
        if n != self._seen_n:
            self._seen_n = n
        age = f.stale_for() or 0.0
        lat = f.transport_lat
        parts = []
        if age > f.STALE_S:
            parts.append('断流 %.1fs' % age)
        else:
            parts.append('在线')
        parts.append('解码 %.1f fps' % self.decode_fps)
        if lat is not None:
            parts.append('传输 %.0f ms' % (lat * 1000))
        parts.append('%s' % f.decoder)
        return '  ·  '.join(parts)


def parse_ep(s):
    """'host' 或 'host:port' → (host, port)"""
    if ':' in s:
        h, p = s.rsplit(':', 1)
        return h, int(p)
    return s, 9102


class Viewer:
    def __init__(self, root, tiles, scale, layout):
        self.root = root
        self.tiles = tiles
        self.scale = scale
        self.layout = layout
        self.quit = False
        root.title('缩略直播查看器')
        root.configure(bg='#101014')
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        self.canvas = tk.Canvas(root, bg='#101014', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<q>', lambda e: self.on_close())
        self.canvas.bind('<Escape>', lambda e: self.on_close())
        self.canvas.bind('<Configure>', lambda e: self.redraw())
        root.bind('<q>', lambda e: self.on_close())
        root.bind('<Escape>', lambda e: self.on_close())
        root.bind('<F11>', self.toggle_fullscreen)
        self._photo = {}
        self._overlay = {}
        for t in tiles:
            t.start()
        self.msg = ''

    def toggle_fullscreen(self, _e=None):
        self.root.attributes('-fullscreen',
                             not self.root.attributes('-fullscreen'))

    def on_close(self):
        self.quit = True

    def _cells(self, w, h):
        """按 tiles 数量算每格的 (x, y, w, h)。"""
        n = len(self.tiles)
        if n == 1:
            cols, rows = 1, 1
        elif n <= 2:
            cols, rows = 2, 1
        elif n <= 4:
            cols, rows = 2, 2
        else:
            cols, rows = 3, (n + 2) // 3
        cw, ch = w // cols, h // rows
        out = []
        for i in range(n):
            c, r = i % cols, i // cols
            out.append((c * cw, r * ch, cw, ch))
        return out

    def redraw(self):
        c = self.canvas
        # 必须先清空：每帧都新建 canvas 元素，不清会无限堆积（实测 30 秒
        # 就攒出十万级 text 项，内存和重绘耗时一起涨）。
        c.delete('all')
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        cells = self._cells(w, h)
        for i, t in enumerate(self.tiles):
            x, y, cw, ch = cells[i]
            img = t.feed.image
            key = i
            if img is None:
                c.create_rectangle(x, y, x + cw, y + ch,
                                   fill='#15151b', outline='#2a2a33')
            else:
                ih, iw = img.shape[:2]
                sc = min(cw / iw, ch / ih)
                dw, dh = max(1, int(iw * sc)), max(1, int(ih * sc))
                # 帧没变就复用上一张 PhotoImage；LiveFeed 每帧都会换 ndarray，
                # 用 image_n 当版本号，避免同一帧重复转换。
                stamp = (dw, dh, t.feed.image_n)
                if self.scale.get(key) != stamp:
                    self.scale[key] = stamp
                    small = cv2.resize(img, (dw, dh),
                                       interpolation=cv2.INTER_AREA)
                    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    # Tk 认 PPM(P6)，不认裸 RGB：手动拼 15 字节头
                    hdr = b'P6\n%d %d\n255\n' % (dw, dh)
                    self._photo[key] = tk.PhotoImage(
                        width=dw, height=dh,
                        data=hdr + rgb.tobytes(), format='ppm')
                ph = self._photo.get(key)
                if ph is not None:
                    c.create_image(x + cw // 2, y + ch // 2, image=ph)
        # 覆盖文字（每帧重画，不缓存）
        for i, t in enumerate(self.tiles):
            x, y, cw, ch = cells[i]
            c.create_rectangle(x, y, x + cw, y + 26,
                               fill='#000000', outline='')
            c.create_text(x + 8, y + 13, anchor='w',
                          text='%s  %s:%d' % (t.name, t.host, t.port),
                          fill='#7fe3a0',
                          font=('DejaVu Sans Mono', 10, 'bold'))
            c.create_text(x + 8, y + ch - 14, anchor='w',
                          text=t.status_line(), fill='#d0d0d8',
                          font=('DejaVu Sans Mono', 9))

    def tick(self):
        for t in self.tiles:
            t.tick_stats()
        self.redraw()
        if not self.quit:
            self.root.after(33, self.tick)


def parse_args():
    ap = argparse.ArgumentParser(description='缩略直播查看器')
    ap.add_argument('--host', action='append', default=None,
                    help='host 或 host:port，可重复（多路）')
    ap.add_argument('--thumb-port', type=int, default=9102)
    ap.add_argument('--display-width', type=int, default=1280)
    ap.add_argument('--window-width', type=int, default=1600)
    ap.add_argument('--window-height', type=int, default=1000)
    return ap.parse_args()


def main():
    args = parse_args()
    eps = args.host or ['192.168.123.2']
    tiles = []
    for i, s in enumerate(eps):
        h, p = parse_ep(s) if ':' in s else (s, args.thumb_port)
        name = 'CAM%d' % (i + 1)
        tiles.append(Tile(name, h, p, args.display_width))

    global tk
    import tkinter
    tk = tkinter

    root = tk.Tk()
    root.geometry('%dx%d' % (args.window_width, args.window_height))
    viewer = Viewer(root, tiles, {}, args)

    def loop():
        viewer.tick()

    root.after(100, loop)
    try:
        root.mainloop()
    finally:
        for t in tiles:
            t.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
