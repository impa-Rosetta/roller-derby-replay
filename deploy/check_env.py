#!/usr/bin/env python3
"""Fail closed when the platform cannot meet the deployment contract."""
import argparse,importlib,subprocess,json,os,platform
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--role',choices=['node','dgx'],required=True);p.add_argument('--require-camera',action='store_true');a=p.parse_args();errors=[];info={'role':a.role,'architecture':platform.machine()}
for name in (['av','cv2','numpy'] if a.role=='node' else ['av','cv2','numpy','tkinter','gi']):
    try:m=importlib.import_module(name);info[name]=getattr(m,'__version__','available')
    except ImportError as e:errors.append(str(e))
if a.role=='node':
    if platform.machine()!='aarch64':errors.append('RK node must be aarch64')
    os.environ.setdefault('MVCAM_COMMON_RUNENV','/opt/MVS/lib')
    import sys;sys.path.insert(0,'/opt/MVS/Samples/aarch64/Python/MvImport')
    try:import MvCameraControl_class;info['mvs']='loaded'
    except Exception as e:errors.append('MVS: '+str(e))
    ff='/usr/local/bin/ffmpeg'
    try:
        proc=subprocess.run([ff,'-hide_banner','-encoders'],capture_output=True,text=True,timeout=10)
        if proc.returncode or 'hevc_rkmpp' not in proc.stdout:errors.append('FFmpeg must include hevc_rkmpp')
        info['ffmpeg']=subprocess.check_output([ff,'-version'],text=True).splitlines()[0]
    except Exception as e:errors.append('FFmpeg: '+str(e))
    cams=[]
    for d in Path('/sys/bus/usb/devices').iterdir():
        try:
            if (d/'idVendor').read_text().strip()=='2bdf':cams.append({'path':d.name,'serial':(d/'serial').read_text().strip(),'Mbps':float((d/'speed').read_text())})
        except OSError:pass
    info['cameras']=cams
    if a.require_camera and (len(cams)!=1 or cams[0]['Mbps']<5000):errors.append('Exactly one Hikrobot camera at USB3 5000Mbps is required')
else:
    try:
        proc=subprocess.run(['gst-inspect-1.0','nvh265dec'],capture_output=True,timeout=10)
        info['NVDEC']=proc.returncode==0
        if proc.returncode:errors.append('nvh265dec is required for validated DGX hardware decoding')
    except Exception as e:errors.append(str(e))
info['errors']=errors;print(json.dumps(info,ensure_ascii=False,indent=2));raise SystemExit(bool(errors))
