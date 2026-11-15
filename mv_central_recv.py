#!/usr/bin/env python3
"""中央接收端（模拟 Spark 判罚节点接收侧）—— 轮滑阻拦赛判罚项目

接收边缘节点的 JPEG 压缩流（帧协议见 mv_edge_node.py）：
  解码校验 + 实时统计（帧率/带宽/传输时延/帧号连续性）

用法:
  /usr/bin/python3 /home/ifit/xjh/mv_central_recv.py [--port 9020] [--save-dir 目录] [--save-n 5]
"""
import argparse
import os
import socket
import struct
import sys
import time

import cv2
import numpy as np

HDR = struct.Struct(">4sdII")
MAGIC = b"MVED"


def recv_exact(sock, n):
    """精确接收 n 字节；对端断开返回 None"""
    chunks = []
    while n > 0:
        c = sock.recv(min(n, 1 << 20))
        if not c:
            return None
        chunks.append(c)
        n -= len(c)
    return b"".join(chunks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9020)
    ap.add_argument("--save-dir", default="", help="存前 N 帧的目录（验画质）")
    ap.add_argument("--save-n", type=int, default=5)
    args = ap.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(f"中央接收端监听 {args.host}:{args.port} ...")

    exit_code = 0
    conn = None
    try:
        conn, addr = server.accept()
        print(f"边缘节点已连接: {addr[0]}:{addr[1]}")

        t0 = None
        last = None
        n = 0
        total = 0
        saved = 0
        last_frame_id = None
        max_gap = 0        # 帧号最大跳跃（丢帧检测）
        lat_sum = 0.0      # 时间戳到接收的时延合计

        while True:
            hdr = recv_exact(conn, HDR.size)
            if hdr is None:
                break
            magic, ts, fid, ln = HDR.unpack(hdr)
            if magic != MAGIC:
                print(f"协议错乱: magic={magic!r}，终止")
                exit_code = 4
                break
            data = recv_exact(conn, ln)
            if data is None:
                break

            now = time.time()
            if t0 is None:
                t0 = now
            if last_frame_id is not None:
                gap = (fid - last_frame_id) & 0xFFFFFFFF
                if gap > 1 and gap - 1 > max_gap:
                    max_gap = gap - 1
            last_frame_id = fid
            lat_sum += now - ts
            n += 1
            total += len(data)

            if args.save_dir and saved < args.save_n:
                img = cv2.imdecode(np.frombuffer(data, np.uint8),
                                   cv2.IMREAD_COLOR)
                if img is not None:
                    os.makedirs(args.save_dir, exist_ok=True)
                    cv2.imwrite(os.path.join(args.save_dir,
                                             f"recv_{saved:03d}.jpg"), img)
                    saved += 1

            if now - (last or t0) >= 2.0:
                d = now - t0
                print(f"  {d:5.1f}s  收到 {n} 帧 ({n / d:.1f} FPS)  "
                      f"{total * 8 / d / 1e6:.1f} Mbps  "
                      f"平均时延 {lat_sum / n * 1000:.1f} ms")
                last = now

        dur = (time.time() - t0) if t0 else 0
        if n:
            print(f"接收结束: {n} 帧 / {dur:.2f}s = {n / dur:.1f} FPS, "
                  f"总 {total / 1e6:.1f} MB, 均帧 {total / n / 1024:.1f} KB, "
                  f"最大丢帧 {max_gap}, 平均时延 {lat_sum / n * 1000:.1f} ms")
            if args.save_dir:
                print(f"存图 {saved} 张 -> {args.save_dir}")
    except KeyboardInterrupt:
        print("\n收到中断信号")
        exit_code = 130
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}")
        exit_code = 3
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            server.close()
        except Exception:
            pass
        print("已安全退出")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
