#!/usr/bin/env python3
"""Read-only boot readiness audit. Does not capture, reset cameras or record."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from launch_monitor import load_nodes

PROBE=r'''
import json,pathlib,subprocess,os,shutil
os.environ['LC_ALL']='C'
expected=EXPECTED
home=pathlib.Path.home()
p=subprocess.run(['/usr/bin/python3',str(home/'hk_camera/check_env.py'),'--role','node','--require-camera'],capture_output=True,text=True,timeout=20)
r=json.loads(p.stdout);r['errors']=list(r.get('errors',[]))
r['boot_id']=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
r['free_gb']=round(shutil.disk_usage(home/'hk_record').free/1e9,2)
if r['free_gb']<10:r['errors'].append('Less than 10GB free for recording')
ips=json.loads(subprocess.check_output(['ip','-j','addr'],text=True))
interface=next((d['ifname'] for d in ips if any(a.get('local')==expected for a in d.get('addr_info',[]))),None)
r['wired_interface']=interface
if not interface or interface.startswith(('wl','wlan')):r['errors'].append('Expected wired IPv4 missing')
else:
 active=subprocess.check_output(['nmcli','-t','-f','UUID,DEVICE','connection','show','--active'],text=True)
 uuid=next((line.split(':')[0] for line in active.splitlines() if line.split(':')[-1]==interface),None)
 r['wired_profile']=uuid
 if uuid:
  values=subprocess.check_output(['nmcli','-g','connection.autoconnect,connection.autoconnect-priority,ipv4.method,ipv4.never-default','connection','show','uuid',uuid],text=True).splitlines()
  r['wired_settings']=values
  if values[0]!='yes' or int(values[1])<1 or values[2]!='manual' or values[3]!='yes':r['errors'].append('Wired autoconnect/static/priority configuration incomplete')
 else:r['errors'].append('Wired NetworkManager profile missing')
status=subprocess.run(['systemctl','is-active','--quiet','roller-replay-performance.service'])
if status.returncode:r['errors'].append('Performance service not active')
print(json.dumps(r))
'''


def check_node(node):
    try:
        resolved=subprocess.check_output(['ssh','-G',node['remote']],text=True,stderr=subprocess.DEVNULL)
        host=next(line.split(None,1)[1] for line in resolved.splitlines() if line.startswith('hostname '))
        if host!=node['host']:return dict(errors=['SSH alias must use configured wired IP: '+host])
        command='python3 -c '+shlex.quote(PROBE.replace('EXPECTED',repr(node['host'])))
        p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=3',node['remote'],command],capture_output=True,text=True,timeout=28)
        if p.returncode:return dict(errors=[p.stderr.strip() or p.stdout.strip() or 'SSH probe failed'])
        return json.loads(p.stdout)
    except Exception as exc:return dict(errors=[str(exc)])


def check_dgx():
    errors=[]
    p=subprocess.run(['/usr/bin/python3',str(REPO/'deploy/check_env.py'),'--role','dgx'],capture_output=True,text=True,timeout=25)
    try:r=json.loads(p.stdout);errors.extend(r.get('errors',[]))
    except ValueError:r={};errors.append(p.stderr.strip() or 'DGX dependency check failed')
    state=Path.home()/'.local/share/roller-derby-replay'
    try:
        health=json.loads((state/'multi-health.json').read_text());r['monitor_state']=health.get('state')
        r['monitor_health_age_seconds']=round(time.time()-health['updated'],1)
    except (OSError,ValueError,KeyError):r['monitor_state']='not running'
    r['boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip();r['errors']=errors
    return r


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(REPO/'nodes.local.json'))
    parser.add_argument('--wait',type=float,default=0,help='Retry until ready or this many seconds have elapsed')
    parser.add_argument('--output',type=Path,default=Path.home()/'.local/share/roller-derby-replay/boot-readiness.json')
    args=parser.parse_args();nodes=load_nodes(args.config);deadline=time.monotonic()+max(0,args.wait)
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)+1) as pool:
            futures={n['id']:pool.submit(check_node,n) for n in nodes};dgx=pool.submit(check_dgx)
            result={key:future.result() for key,future in futures.items()};result['dgx']=dgx.result()
        ready=all(not value['errors'] for value in result.values())
        report=dict(updated=time.time(),ready=ready,scope='device environment; monitor launched manually',devices=result)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        temp=args.output.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2));temp.replace(args.output)
        print(json.dumps(dict(ready=ready,errors={k:v['errors'] for k,v in result.items()}),ensure_ascii=False),flush=True)
        if ready or time.monotonic()>=deadline:return 0 if ready else 1
        time.sleep(min(5,max(0,deadline-time.monotonic())))


if __name__=='__main__':raise SystemExit(main())
