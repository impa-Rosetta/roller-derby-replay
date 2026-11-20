#!/usr/bin/env python3
import argparse,subprocess,sys,shlex
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));from launch_monitor import load_nodes
p=argparse.ArgumentParser();p.add_argument('--config',default='nodes.local.json');p.add_argument('--node',required=True);a=p.parse_args();n=next(n for n in load_nodes(a.config) if n['id']==a.node);host=n['remote'];r=Path(__file__).resolve().parents[1]
def ssh(cmd):subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=6',host,cmd],check=True)
# Refuse to replace code during a live recording.
ssh("if pgrep -f '[p]ython3.*hk_pipeline_rk.py' >/dev/null; then echo 'Stop recording before deployment'; exit 1; fi; mkdir -p ~/hk_camera; if test -f ~/hk_camera/hk_pipeline_rk.py; then cp -p ~/hk_camera/hk_pipeline_rk.py ~/hk_camera/hk_pipeline_rk.py.backup-$(date +%Y%m%d_%H%M%S); fi")
for src,dst in [('rk3588_deploy/hk_pipeline_rk.py','hk_pipeline_rk.py'),('deploy/bootstrap.sh','bootstrap.sh'),('deploy/check_env.py','check_env.py')]:subprocess.run(['scp',str(r/src),host+':hk_camera/'+dst],check=True)
ssh('bash ~/hk_camera/bootstrap.sh node')
ssh('MVCAM_COMMON_RUNENV=/opt/MVS/lib LD_LIBRARY_PATH=/opt/MVS/lib/aarch64 /usr/bin/python3 ~/hk_camera/check_env.py --role node --require-camera')
