"""Unified DGX/RK recording inventory, verified archival and explicit deletion."""
import concurrent.futures
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import storage_agent as agent


class StorageService:
    def __init__(self, nodes, archive_roots, state_root):
        self.nodes = {n['id']: n for n in nodes}
        self.archive_roots = {k: str(Path(v).expanduser().resolve()) for k, v in archive_roots.items()}
        self.state_root = Path(state_root)

    def call(self, node, location, action, **kwargs):
        root = (self.nodes[node].get('remote_root', '~/hk_record') if location == 'rk'
                else self.archive_roots[node])
        request = dict(root=root, action=action, remote_recordings=location == 'rk', **kwargs)
        if location == 'dgx':
            return agent.execute(request)
        # Source travels through the existing SSH connection; no node install.
        source = Path(agent.__file__).read_text()
        code = 'import sys,io;sys.stdin=io.StringIO('+repr(json.dumps(request))+');'+source
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                                 self.nodes[node]['remote'],
                                 'python3 -c '+shlex.quote(code)],
                                capture_output=True, text=True, timeout=900 if action == 'manifest' else 60)
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError(result.stderr.strip()[-400:] or '节点未返回存储信息')
        if 'error' in payload:
            raise RuntimeError(payload['error'])
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-400:] or '存储操作失败')
        return payload['result']

    def snapshot(self):
        disks, rows, errors = {}, [], {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs = {(n, loc): pool.submit(self.call, n, loc, 'scan')
                    for n in self.nodes for loc in ('rk', 'dgx')}
            for (node, location), future in jobs.items():
                try:
                    data = future.result()
                    disks[(node if location == 'rk' else 'dgx')] = data
                    for row in data['sessions']:
                        row.update(node=node, location=location,
                                   id=node+'|'+location+'|'+row['name'])
                        rows.append(row)
                    if data['errors']:
                        errors[node+'|'+location] = '; '.join(data['errors'])
                except Exception as exc:
                    errors[node+'|'+location] = str(exc)
        rows.sort(key=lambda r: (r['name'], r['node'], r['location']), reverse=True)
        copies = {(r['node'], r['name'], r['location']): r for r in rows}
        for row in rows:
            other = copies.get((row['node'], row['name'], 'dgx' if row['location'] == 'rk' else 'rk'))
            row['other_copy'] = bool(other)
            row['archive_verified'] = bool((other if row['location'] == 'rk' else row) and
                                          (other if row['location'] == 'rk' else row).get('verified'))
        return dict(disks=disks, rows=rows, errors=errors, measured=time.time())

    def audit(self, action, row, status, detail):
        self.state_root.mkdir(parents=True, exist_ok=True)
        with (self.state_root/'storage-operations.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(time=time.time(), action=action, node=row['node'],
                                        location=row['location'], name=row['name'],
                                        status=status, detail=detail), ensure_ascii=False)+'\n')

    def delete(self, row):
        try:
            result = self.call(row['node'], row['location'], 'delete',
                               name=row['name'], identity=row['identity'])
        except Exception as exc:
            self.audit('delete', row, 'FAIL', str(exc))
            raise
        self.audit('delete', row, 'OK', result)
        return result

    def archive(self, row, progress=lambda text: None):
        if row['location'] != 'rk':
            raise ValueError('请选择 RK 原件进行归档')
        node, name = row['node'], row['name']
        root = Path(self.archive_roots[node]);root.mkdir(parents=True, exist_ok=True)
        final, part = root/name, root/('.'+name+'.partial')
        for p in (final, part):
            if p.is_symlink() or p.resolve().parent != root:
                raise ValueError('归档目标路径无效')
        progress('读取 RK 文件校验值…')
        source = self.call(node, 'rk', 'manifest', name=name, identity=row['identity'])
        if not any(n.endswith('.h265') for n in source['files']):
            raise ValueError('录像没有可归档的视频分段')
        if not final.exists():
            part.mkdir(exist_ok=True)
            existing = agent.inventory(part)
            need = sum(v['bytes'] for k, v in source['files'].items()
                       if existing.get(k) != {'bytes': v['bytes']})
            if shutil.disk_usage(root).free < need+10_000_000_000:
                raise ValueError('DGX 空间不足，归档需要保留至少 10 GB 可用空间')
            progress('正在传输至 DGX（可断点续传）…')
            command = ['rsync', '-a', '--checksum', '--partial', '--partial-dir=.rsync-partial',
                       '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=6',
                       self.nodes[node]['remote']+':'+shlex.quote(row['directory']+'/'), str(part)+'/']
            result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
            if result.returncode:
                raise RuntimeError('归档传输失败：'+result.stderr[-400:])
        destination = final if final.exists() else part
        progress('核对 DGX 与 RK 的 SHA-256 校验值…')
        copied = agent.inventory(destination, hashes=True)
        current = self.call(node, 'rk', 'manifest', name=name, identity=row['identity'])
        if source != current or copied != source['files']:
            raise ValueError('归档校验失败：文件内容不一致，未标记为完整归档')
        if destination == part:
            part.rename(final)
        proof = dict(node=node, source=row['directory'], verified=time.time(), files=copied,
                     mtime_ns={k: (final/k).stat().st_mtime_ns for k in copied})
        temp = final/(agent.PROOF+'.tmp')
        temp.write_text(json.dumps(proof));temp.replace(final/agent.PROOF)
        self.audit('archive', row, 'OK', str(final))
        return dict(directory=str(final), verified=True)
