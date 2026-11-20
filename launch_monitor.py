#!/usr/bin/env python3
"""One isolated DGX monitor per configured RK3588 node."""
import argparse,json,os,subprocess,sys,fcntl
from pathlib import Path

def load_nodes(path):
    nodes=json.loads(Path(path).read_text())['nodes']
    if not nodes: raise ValueError('No nodes configured')
    for field in ('id','host','referee_port'):
        vals=[n[field] for n in nodes]
        if len(vals)!=len(set(vals)): raise ValueError('Duplicate '+field)
    for n in nodes:
        if not n['id'].replace('-','').replace('_','').isalnum(): raise ValueError('Invalid node id')
        if not 1024<=int(n['referee_port'])<=65535: raise ValueError('Invalid referee port')
    return nodes

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default=str(Path(__file__).with_name('nodes.local.json')));p.add_argument('--node',required=True);p.add_argument('--print-command',action='store_true');a,extra=p.parse_known_args()
    node=next((n for n in load_nodes(a.config) if n['id']==a.node),None)
    if node is None:p.error('Unknown node '+a.node)
    state=Path.home()/'.local/share/roller-derby-replay'/a.node;state.mkdir(parents=True,exist_ok=True)
    settings=state/'state';settings.mkdir(exist_ok=True)
    archive=settings/'archive.json'
    if not archive.exists():archive.write_text(json.dumps({'dir':str(Path.home()/'录像_rk'/a.node)},ensure_ascii=False))
    cmd=['/usr/bin/python3',str(Path(__file__).with_name('studio_app_rk.py')),'--host',node['host'],'--remote',node['remote'],'--remote-root',node.get('remote_root','~/hk_record'),'--live-port',str(node.get('live_port',9101)),'--thumb-port','0','--referee-port',str(node['referee_port']),'--cache-dir',str(state/'cache'),'--out-dir',str(state/'state')]+extra
    if a.print_command:print(json.dumps(cmd,ensure_ascii=False));return 0
    with (state/'monitor.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('Monitor already running for '+a.node);return 0
        with (state/'monitor.log').open('a') as log:return subprocess.call(cmd,cwd=Path(__file__).parent,stdout=log,stderr=subprocess.STDOUT)
if __name__=='__main__':raise SystemExit(main())
