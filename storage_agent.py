"""Restricted recording filesystem operations, also executed on RK via stdin.

No shell deletion: only direct, non-symlink stage1_* children of a configured
recording root may be removed. A refreshed identity guards stale selections.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

SESSION = re.compile(r'stage1_\d{8}_\d{6}$')
PROOF = '.storage-verified.json'


def recording_running(proc_root='/proc'):
    for p in Path(proc_root).glob('[0-9]*'):
        try:
            args = (p/'cmdline').read_bytes().split(b'\0')
            if any(Path(a.decode(errors='replace')).name == 'hk_pipeline_rk.py' for a in args):
                return True
        except OSError:
            pass
    return False


def checked_directory(root, name):
    if not SESSION.fullmatch(name):
        raise ValueError('无效的录像目录名')
    root = Path(root).expanduser().resolve()
    p = root/name
    if p.is_symlink() or not p.is_dir() or p.resolve().parent != root:
        raise ValueError('录像不存在，或路径不属于录像目录')
    return p


def inventory(p, hashes=False):
    result = {}
    for parent, dirs, files in os.walk(p, followlinks=False):
        for name in dirs + files:
            if (Path(parent)/name).is_symlink():
                raise ValueError('录像目录包含符号链接，禁止操作')
        for name in files:
            f = Path(parent)/name
            relative = f.relative_to(p).as_posix()
            if relative == PROOF or '.rsync-partial' in f.relative_to(p).parts:
                continue
            stat = f.stat()
            if not f.is_file():
                raise ValueError('录像包含非常规文件')
            entry = {'bytes': stat.st_size}
            if hashes:
                digest = hashlib.sha256()
                with f.open('rb') as stream:
                    for block in iter(lambda: stream.read(4*1024*1024), b''):
                        digest.update(block)
                entry['sha256'] = digest.hexdigest()
            result[relative] = entry
    return result


def identity(p):
    s = p.stat()
    meta = p/'meta.jsonl'
    m = meta.stat() if meta.exists() else None
    return [s.st_dev, s.st_ino, s.st_mtime_ns,
            m.st_size if m else 0, m.st_mtime_ns if m else 0]


def read_segments(p):
    result = []
    try:
        lines = (p/'meta.jsonl').read_text().splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            s = json.loads(line)
            f = s['file']
            if Path(f).name != f or not f.endswith('.h265'):
                continue
            path = p/f
            if path.is_symlink() or not path.is_file():
                continue
            if s['end_ms'] <= s['start_ms'] or s.get('frames', 0) <= 0:
                continue
            s.update(dir=str(p), start_s=s['start_ms']/1000,
                     end_s=s['end_ms']/1000, bytes=path.stat().st_size)
            result.append(s)
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return sorted(result, key=lambda s: s['start_s'])


def session_info(root, name, busy=False):
    p = checked_directory(root, name)
    files = inventory(p)
    segments = read_segments(p)
    duration = sum(s['end_s']-s['start_s'] for s in segments)
    stamp = min((s['base_ms']/1000 for s in segments if s.get('base_ms')), default=0)
    verified = False
    try:
        proof_path = p/PROOF
        proof = json.loads(proof_path.read_text())
        verified = (files == {k: {'bytes': v['bytes']} for k, v in proof['files'].items()} and
                    {k: (p/k).stat().st_mtime_ns for k in files} == proof['mtime_ns'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return dict(name=name, directory=str(p), identity=identity(p),
                bytes=sum(v['bytes'] for v in files.values()),
                frames=sum(s['frames'] for s in segments), seconds=duration,
                start=stamp, segments=segments, busy=busy,
                verified=verified)


def execute(request):
    root = Path(request['root']).expanduser().resolve()
    action = request['action']
    busy = recording_running() if request.get('remote_recordings') else False
    if action == 'scan':
        existing = root
        while not existing.exists():
            existing = existing.parent
        disk = shutil.disk_usage(existing)
        sessions, errors = [], []
        if root.is_dir():
            for p in sorted(root.iterdir(), reverse=True):
                if SESSION.fullmatch(p.name):
                    try:
                        sessions.append(session_info(root, p.name, busy))
                    except (OSError, ValueError) as exc:
                        errors.append(p.name+': '+str(exc))
        return dict(total=disk.total, used=disk.used, available=disk.free,
                    root=str(root), busy=busy, sessions=sessions, errors=errors)
    p = checked_directory(root, request['name'])
    if request.get('identity') != identity(p):
        raise ValueError('录像内容已变化，请刷新列表后重新操作')
    if busy:
        raise ValueError('该 RK 正在录制，请停止该节点录制后再操作录像')
    if action == 'manifest':
        before = identity(p)
        files = inventory(p, hashes=True)
        if identity(p) != before or (request.get('remote_recordings') and recording_running()):
            raise ValueError('录像在校验期间变化，请停止录制后重试')
        return dict(files=files, identity=before)
    if action == 'delete':
        # Inspect every child before deletion; do not traverse a symlink.
        files = inventory(p)
        if identity(p) != request['identity']:
            raise ValueError('录像内容已变化，请刷新后重试')
        if request.get('remote_recordings') and recording_running():
            raise ValueError('检测到录制刚刚启动，已取消删除')
        try:
            shutil.rmtree(p)
        except OSError as exc:
            raise RuntimeError('删除未完成，目录可能已部分清理：'+str(exc)) from exc
        return dict(deleted=True, bytes=sum(v['bytes'] for v in files.values()),
                    directory=str(p), time=time.time())
    raise ValueError('不支持的存储操作')


if __name__ == '__main__':
    try:
        print(json.dumps({'result': execute(json.loads(sys.stdin.read()))}))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
