"""Camera-independent match indexing and a single clock for four-angle replay."""
from dataclasses import dataclass, field
from bisect import bisect_right
import json
import math
from pathlib import Path
import shlex
import subprocess
import time
import concurrent.futures
import logging

LOG=logging.getLogger(__name__)


def remote_python(remote, source, timeout=30):
    command = '/usr/bin/python3 -c ' + shlex.quote(source)
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                             remote, command], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'SSH failed')
    return result.stdout


def camera_catalog(node, local_root=None):
    root = node.get('remote_root', '~/hk_record')
    source = '''from pathlib import Path
import json
root=Path(ROOT).expanduser()
sessions=[]
active=False
for process in Path('/proc').glob('[0-9]*'):
 try:
  args=(process/'cmdline').read_bytes().split(bytes([0]))
  if any(Path(a.decode(errors='replace')).name=='hk_pipeline_rk.py' for a in args):active=True;break
 except OSError:pass
directories=sorted(root.glob('stage1_*'),reverse=True)
for d in directories:
 p=d/'meta.jsonl'
 rows=[]
 for line in (p.read_text().splitlines() if p.exists() else []):
  try:
   r=json.loads(line)
   if r.get('end_ms',0)>r.get('start_ms',0) and r.get('frames',0)>0:
    r['dir']=str(d);r['start_s']=r['start_ms']/1000;r['end_s']=r['end_ms']/1000
    rows.append(r)
  except (ValueError,KeyError):pass
 sessions.append(dict(name=d.name,dir=str(d),segments=rows,active=active and d==directories[0]))
print(json.dumps(sessions))
'''.replace('ROOT', repr(root))
    local = []
    if local_root:
        from storage_agent import read_segments, SESSION
        root = Path(local_root).expanduser()
        if root.is_dir():
            for directory in sorted(root.iterdir(), reverse=True):
                if directory.is_symlink() or not directory.is_dir() or not SESSION.fullmatch(directory.name):continue
                segments = read_segments(directory)
                for segment in segments:segment['local_dir'] = str(directory)
                if segments:local.append(dict(name=directory.name,dir=str(directory),segments=segments,active=False))
    try:
        remote = json.loads(remote_python(node['remote'], source))
    except Exception:
        if not local:raise
        LOG.warning('Node %s unavailable; using DGX archives', node['id'])
        for session in local:session['node_offline']=True
        return local
    names = {s['name'] for s in remote}
    return remote+[s for s in local if s['name'] not in names]


def measure_clock(node, samples=7):
    """Exclude SSH/Python startup from RTT by exchanging lines after READY."""
    source = "import sys,time\nprint('READY',flush=True)\nfor line in sys.stdin:\n print(time.time_ns(),flush=True)"
    process = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                                node['remote'], '/usr/bin/python3 -u -c '+shlex.quote(source)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # This function runs in a background job. A watchdog bounds a disconnected
    # SSH child's reads, including the initial readiness line.
    import threading
    timer = threading.Timer(10, process.kill)
    timer.start()
    try:
        if process.stdout.readline().strip() != 'READY':
            raise RuntimeError('时钟测量连接失败')
        observations = []
        for _ in range(samples):
            t0 = time.time()
            process.stdin.write('PING\n'); process.stdin.flush()
            line = process.stdout.readline()
            t1 = time.time()
            remote = int(line)/1e9
            observations.append((t1-t0, remote-(t0+t1)/2, remote))
        rtt, offset, remote = min(observations)
        return dict(offset=offset, uncertainty=rtt/2, remote=remote, measured=time.time())
    finally:
        timer.cancel()
        process.stdin.close()
        process.wait(timeout=2)
        process.stdout.close()


@dataclass
class Track:
    node: str
    name: str
    directory: str
    segments: list
    offset: float = 0.0  # node clock minus DGX clock, plus manual alignment
    calibrated: bool = False
    uncertainty: float = 0.0
    clock_samples: list = field(default_factory=list)
    _signature: object = field(default=None,init=False,repr=False)
    _ranges: list = field(default_factory=list,init=False,repr=False)
    _wall_starts: list = field(default_factory=list,init=False,repr=False)
    _source_starts: list = field(default_factory=list,init=False,repr=False)

    @classmethod
    def from_session(cls, node, session):
        return cls(node, session['name'], session['dir'], session['segments'])

    def correction(self, raw_wall):
        samples = sorted(self.clock_samples, key=lambda s: s['remote'])
        if not samples:
            return self.offset
        if raw_wall <= samples[0]['remote']:
            return samples[0]['offset'] + self.offset
        for left, right in zip(samples, samples[1:]):
            if raw_wall <= right['remote']:
                k = (raw_wall-left['remote']) / max(1e-9, right['remote']-left['remote'])
                return left['offset']*(1-k) + right['offset']*k + self.offset
        return samples[-1]['offset'] + self.offset

    def bounds(self, seg):
        raw = seg['base_ms']/1000
        start = raw - self.correction(raw)
        raw_end = raw + seg['end_s']-seg['start_s']
        return start, raw_end-self.correction(raw_end)

    def intervals(self):
        signature=(id(self.segments),len(self.segments),self.offset,
                   tuple((s['remote'],s['offset']) for s in self.clock_samples))
        if signature!=self._signature:
            self._signature=signature
            self._ranges=[self.bounds(s) for s in self.segments]
            self._wall_starts=[a for a,b in self._ranges]
            self._source_starts=[s['start_s'] for s in self.segments]
        return self._ranges

    @property
    def start(self):
        return min(a for a,b in self.intervals()) if self.segments else 0

    @property
    def end(self):
        return max(b for a,b in self.intervals()) if self.segments else 0

    def locate(self, wall):
        ranges=self.intervals()
        i=bisect_right(self._wall_starts,wall)-1
        # A live ring snapshot may overlap already sealed disk segments and
        # extend past their ends. Walk back only when the newest interval ends.
        while i>=0:
            a,b=ranges[i];seg=self.segments[i]
            if a<=wall<b:
                return seg,seg['start_s']+(wall-a)*(seg['end_s']-seg['start_s'])/(b-a)
            i-=1
        return None

    def wall_for_position(self, position):
        ranges=self.intervals()
        i=bisect_right(self._source_starts,position)-1
        if i>=0:
            seg=self.segments[i]
            if seg['start_s'] <= position < seg['end_s']:
                a,b = ranges[i]
                return a+(position-seg['start_s'])*(b-a)/(seg['end_s']-seg['start_s'])
        return None

    def dump(self):
        return dict(node=self.node,name=self.name,directory=self.directory,
                    segments=self.segments,offset=self.offset,calibrated=self.calibrated,
                    uncertainty=self.uncertainty,clock_samples=self.clock_samples)


def grouped_sessions(catalogs):
    """Match actual capture times, not each camera's newest directory.

    Prefer complete long groups over a later short accidental recording.
    The caller can still explicitly select any camera directory.
    """
    tracks = {key: [Track.from_session(key,s) for s in sessions
                    if s['segments'] and s['segments'][0].get('base_ms')]
              for key,sessions in catalogs.items()}
    groups, seen = [], set()
    for candidates in tracks.values():
        for anchor in candidates:
            group = {}
            for key, options in tracks.items():
                nearby = [t for t in options if abs(t.start-anchor.start)<120]
                if nearby:
                    group[key] = min(nearby,key=lambda t:abs(t.start-anchor.start))
            signature=tuple(sorted((k,t.directory) for k,t in group.items()))
            if signature in seen:continue
            seen.add(signature)
            if group:
                groups.append(group)
    return sorted(groups,key=lambda g:(len(g),min(t.end-t.start for t in g.values()),
                                       min(t.start for t in g.values())),reverse=True)


class ReplayCoordinator:
    def __init__(self, controllers, now=time.monotonic, background=False):
        self.controllers = controllers
        self.now = now
        self.tracks = {}
        self.origin = 0
        self.state = 'live'
        self.position = 0
        self.anchor = now()
        self.speed = 1.0
        self.linked = True
        self.selected = next(iter(controllers))
        self.targets = {}
        self.messages = {}
        self.resume_after_load = False
        self.load_started = 0
        self.generation = 0
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=4) if background else None
        self.prepares={}
        self.neighbors={}
        self.buffer_keys=set()
        self.failed_files={}
        self.recoveries={}
        self.recovery_attempts={}

    @staticmethod
    def release_future(future):
        def release(f):
            try:f.result()['cap'].release()
            except Exception:pass
        if not future.cancel():future.add_done_callback(release)

    def discard_prepares(self):
        for future in list(self.prepares.values())+list(self.neighbors.values())+list(self.recoveries.values()):
            self.release_future(future)
        self.prepares.clear();self.neighbors.clear();self.recoveries.clear()

    def shutdown(self):
        self.discard_prepares()
        if self.pool:self.pool.shutdown(wait=False,cancel_futures=True)

    def prepare_capture(self,key,seg,info,pos):
        import cv2
        ctl=self.controllers[key]
        cap=ctl._open_capture(info)
        try:
            fps=info['fps']
            index=max(0,min(info['frames']-1,round((pos-seg['start_s'])*fps)))
            if not cap.set(cv2.CAP_PROP_POS_FRAMES,index):
                raise RuntimeError('目标帧定位失败')
            ok,frame=cap.read()
            if not ok:raise RuntimeError('无法读取目标帧')
            return dict(cap=cap,drawn=ctl._fit(frame),idx=index+1,fps=fps,
                        seg=seg,info=info)
        except Exception as exc:
            cap.release()
            # Initialization can succeed but the first accurate hardware seek
            # can still fail. Test the same source via software before calling
            # the recording undecodable; never substitute another moment.
            from replay_capture_rk import HardwareReplayCapture
            if not isinstance(cap,HardwareReplayCapture):raise
            LOG.warning('%s hardware first-frame failed for %s: %s; trying software',key,seg['file'],exc)
            fallback=cv2.VideoCapture(info['mkv'])
            try:
                if not fallback.set(cv2.CAP_PROP_POS_FRAMES,index):raise RuntimeError('软件定位失败')
                ok,frame=fallback.read()
                if not ok:raise RuntimeError('硬件和软件均无法读取目标帧')
                return dict(cap=fallback,drawn=ctl._fit(frame),idx=index+1,fps=fps,seg=seg,info=info)
            except Exception:
                fallback.release();raise

    def fail(self,key,seg,message):
        repeated=self.messages.get(key)==message and self.failed_files.get(key)==seg['file']
        self.messages[key]=message
        self.failed_files[key]=seg['file']
        if not repeated:LOG.warning('Replay failure node=%s file=%s position=%.3f: %s',key,seg['file'],self.playhead(),message)

    def recover(self,key,seg,pos):
        """Retry a transient failure independently; a bad segment cannot latch
        the camera forever after the common clock enters a different segment.
        """
        ctl=self.controllers[key]
        identity=(key,seg['file'])
        if self.failed_files.get(key)!=seg['file']:
            future=self.recoveries.pop(key,None)
            if future is not None:self.release_future(future)
            self.failed_files[key]=seg['file']
        future=self.recoveries.get(key)
        if future is not None:
            if not future.done():return False
            self.recoveries.pop(key)
            try:
                self.install_capture(key,future.result())
                self.messages.pop(key,None);self.failed_files.pop(key,None)
                LOG.info('Replay recovered node=%s file=%s',key,seg['file'])
                return True
            except Exception as exc:self.fail(key,seg,'解码失败：'+str(exc))
        attempts,last=self.recovery_attempts.get(identity,(0,-100))
        if attempts>=2 or self.now()-last<1:return False
        info=ctl.loader.clip(seg['file'])
        if info is None:
            if not ctl.loader.error(seg['file']):ctl.loader.request(seg)
            return False
        self.recovery_attempts[identity]=(attempts+1,self.now())
        if self.pool is None:
            try:
                ctl._open_seg(seg,info,pos);self.messages.pop(key,None);self.failed_files.pop(key,None)
                return ctl.cap is not None
            except Exception as exc:self.fail(key,seg,'解码失败：'+str(exc));return False
        self.messages[key]='正在恢复该路画面…'
        self.recoveries[key]=self.pool.submit(self.prepare_capture,key,seg,info,pos)
        return False

    def install_capture(self,key,prepared):
        ctl=self.controllers[key]
        if ctl.cap is not None:
            old=ctl.cap
            if self.pool is not None:self.pool.submit(old.release)
            else:old.release()
        ctl.cap=prepared['cap'];ctl.drawn=prepared['drawn'];ctl.idx=prepared['idx']
        ctl.seg=prepared['seg'];ctl.fps=prepared['fps'];ctl.frame_count=prepared['info']['frames']
        ctl.pending=None;ctl._next_tick=0;ctl._eof_t=0;ctl.ended=False;ctl.paused=True
        ctl.pos_s=ctl.seg['start_s']+ctl.idx/ctl.fps
        nxt=ctl._next_seg(ctl.seg)
        if nxt is not None:ctl.loader.request(nxt,idle_only=True)

    def prefetch_neighbors(self):
        if self.pool is None:return
        for key in self.participants:
            ctl=self.controllers[key]
            if key in self.messages or ctl.seg is None or key in self.neighbors:continue
            track=self.tracks[key]
            nxt=next((s for s in track.segments if s['start_s']>=ctl.seg['end_s']-.001),None)
            if nxt is None or nxt['file']==ctl.seg['file']:continue
            info=ctl.loader.clip(nxt['file'])
            if info is not None:
                self.neighbors[key]=self.pool.submit(self.prepare_capture,key,nxt,info,nxt['start_s'])
            else:ctl.loader.request(nxt,idle_only=True)

    def set_tracks(self, tracks):
        self.discard_prepares()
        self.generation += 1
        self.tracks = tracks
        self.origin = min((t.start for t in tracks.values() if t.segments), default=0)
        self.position = 0
        self.targets.clear()
        self.messages.clear()
        self.failed_files.clear();self.recovery_attempts.clear()
        self.state = 'live'

    @property
    def duration(self):
        return max((t.end-self.origin for t in self.tracks.values() if t.segments),default=0)

    def rebase(self):
        position=self.playhead()
        origin=min((t.start for t in self.tracks.values() if t.segments),default=self.origin)
        delta=self.origin-origin
        self.origin=origin;self.position=position+delta;self.anchor=self.now()
        return delta

    @property
    def participants(self):
        return list(self.tracks) if self.linked else ([self.selected] if self.selected in self.tracks else [])

    def playhead(self):
        if self.state == 'playing':
            return min(self.duration, self.position + (self.now()-self.anchor)*self.speed)
        return self.position

    def bind(self, key):
        ctl, track = self.controllers[key], self.tracks[key]
        if ctl.opened is not None and ctl.opened.get('dir') == track.directory:
            ctl.opened['segs'] = track.segments
            return
        ctl.close_session(quiet=True)
        ctl.opened = dict(name=track.name,dir=track.directory,
                          root=str(Path(track.directory).parent),segs=track.segments,
                          base=min((s['base_ms']-s['start_ms'] for s in track.segments),default=None),
                          sports=True)
        ctl._marks=[];ctl._marks_loaded=True;ctl._marks_loaded_for=track.name

    def seek(self, position, resume=True):
        self.discard_prepares()
        self.generation += 1
        self.position = max(0,min(self.duration-0.001,position)) if self.duration else 0
        self.anchor = self.now()
        self.state = 'loading'
        self.resume_after_load = resume
        self.load_started = self.now()
        self.targets = {}
        self.messages = {}
        self.failed_files.clear();self.recovery_attempts.clear()
        LOG.info('Replay seek generation=%s position=%.3f resume=%s',self.generation,self.position,resume)
        for key in self.participants:
            ctl = self.controllers[key]
            if hasattr(ctl.loader,'clear_pending'):ctl.loader.clear_pending()
            self.bind(key)
            ctl.mode='replay';ctl.paused=True;ctl.ended=False;ctl.pending=None
            ctl.drawn=None;ctl._jump_t=None
            located = self.tracks[key].locate(self.origin+self.position)
            if located is None:
                self.messages[key]='该时刻无录像'
                continue
            seg,pos = located
            self.targets[key]=(seg,pos)
            if ctl.loader.clip(seg['file']) is None:
                ctl.loader.request(seg)
            self.messages[key]='定位中…'

    def live(self):
        self.discard_prepares()
        self.generation += 1
        self.targets.clear()
        self.messages.clear()
        self.state='live'
        self.failed_files.clear();self.recovery_attempts.clear()
        for ctl in self.controllers.values():
            ctl.to_live()

    def pause(self):
        if self.state=='live':
            self.recent(1,False)
        elif self.state in ('loading','buffering'):
            self.resume_after_load = not self.resume_after_load
        elif self.state=='playing':
            self.position=self.playhead();self.state='paused'
            for ctl in self.controllers.values():ctl.paused=True
        else:
            if self.position>=self.duration-.02:
                self.seek(0,True)
            else:self.anchor=self.now();self.state='playing'

    def set_speed(self, speed):
        self.position=self.playhead();self.anchor=self.now();self.speed=speed

    def step(self, frames):
        self.seek(self.playhead()+frames/59.2,False)

    def recent(self, seconds, resume=True):
        ends=[self.tracks[k].end for k in self.participants if self.tracks[k].segments]
        if ends:self.seek(max(0,min(ends)-self.origin-seconds),resume)

    def tick(self):
        if self.state == 'loading':
            ready={};waiting=False
            for key,(seg,pos) in self.targets.items():
                ctl=self.controllers[key]
                info=ctl.loader.clip(seg['file'])
                error=ctl.loader.error(seg['file'])
                loading=ctl.loader.loading_name() if hasattr(ctl.loader,'loading_name') else ''
                retrying=loading==seg['file'] and not (error or '').startswith('SegmentCorrupt')
                if info is None and (not error or retrying) and self.now()-self.load_started<35:
                    waiting=True;continue
                if info is None:self.fail(key,seg,'片段不可用：'+(error or '加载超时'))
                elif self.pool is None:ready[key]=(seg,info,pos)
                else:
                    if key not in self.prepares:
                        self.prepares[key]=self.pool.submit(self.prepare_capture,key,seg,info,pos)
                    future=self.prepares[key]
                    if not future.done():waiting=True;continue
                    try:ready[key]=future.result()
                    except Exception as exc:self.fail(key,seg,'解码失败：'+str(exc))
            if waiting:return
            for key,value in ready.items():
                ctl=self.controllers[key]
                try:
                    if self.pool is None:
                        seg,info,pos=value;ctl._open_seg(seg,info,pos)
                    else:
                        self.install_capture(key,value);self.prepares.pop(key,None)
                    ctl.paused=True
                    if ctl.cap is None or ctl.drawn is None:raise RuntimeError('无法解码目标帧')
                    self.messages.pop(key,None)
                except Exception as exc:
                    self.fail(key,self.targets[key][0],'解码失败：'+str(exc))
                    ctl.drawn=None
            self.targets.clear()
            self.anchor=self.now()
            self.buffer_keys=set()
            if self.pool is not None and self.resume_after_load:
                for key in ready:
                    if key in self.messages:continue
                    ctl=self.controllers[key]
                    if (ctl.seg and self.tracks[key].bounds(ctl.seg)[1]-(self.origin+self.position)<6
                            and ctl._next_seg(ctl.seg) is not None):self.buffer_keys.add(key)
            self.state='buffering' if self.buffer_keys else ('playing' if self.resume_after_load else 'paused')
            return
        if self.state=='buffering':
            self.prefetch_neighbors()
            for key in self.buffer_keys:
                future=self.neighbors.get(key)
                if future is None or not future.done():
                    if self.now()-self.load_started<35:return
                    LOG.warning('Next segment buffer timeout node=%s; keeping current segment playable',key)
                elif future.exception() is not None:
                    LOG.warning('Next segment buffer failed node=%s: %s',key,future.exception())
            self.anchor=self.now()
            self.state='playing' if self.resume_after_load else 'paused'
            return
        if self.state in ('playing','paused'):self.prefetch_neighbors()
        if self.state=='paused':
            for key in self.participants:
                located=self.tracks[key].locate(self.origin+self.position)
                if key in self.failed_files and located:self.recover(key,*located)
        if self.state != 'playing':return
        position=self.playhead()
        if position>=self.duration-.001:
            self.position=self.duration;self.state='paused'
            for ctl in self.controllers.values():ctl.paused=True
            return
        for key in self.participants:
            ctl=self.controllers[key]
            located=self.tracks[key].locate(self.origin+position)
            if located is None:
                ctl.drawn=None;self.messages[key]='该时刻无录像';continue
            seg,pos=located
            if key in self.messages and not self.messages[key].startswith('该时刻'):
                if not self.recover(key,seg,pos):continue
            self.messages.pop(key,None)
            if (ctl.seg is None or ctl.seg['file']!=seg['file'] or ctl.cap is None):
                neighbor=self.neighbors.get(key)
                if neighbor is not None and neighbor.done():
                    try:
                        prepared=neighbor.result()
                        if prepared['seg']['file']!=seg['file']:
                            self.seek(position,True);return
                        self.neighbors.pop(key)
                        self.install_capture(key,prepared)
                    except Exception as exc:
                        self.neighbors.pop(key,None)
                        self.fail(key,seg,'解码失败：'+str(exc));continue
                else:
                    self.seek(position,True);return
            fps=ctl.fps
            # FPS is derived from packet timestamps. Rounding near the final
            # half-frame must never request frame N+1 from an N-frame segment.
            frame_limit=getattr(ctl,'frame_count',0) or seg['frames']
            desired=min(frame_limit,max(1,int(round((pos-seg['start_s'])*fps))+1))
            due=max(0,desired-ctl.idx)
            if due>12:
                import cv2
                target=desired-1
                ok=ctl.cap.set(cv2.CAP_PROP_POS_FRAMES,target)
                ok,frame=ctl.cap.read() if ok else (False,None)
                if not ok:
                    self.fail(key,seg,'回放定位失败，等待重试');continue
                ctl.idx=target+1;ctl.drawn=ctl._fit(frame)
                ctl.pos_s=seg['start_s']+ctl.idx/fps
            else:
                for _ in range(min(6,due)):
                    ok,frame=ctl.cap.read()
                    if not ok:
                        self.fail(key,seg,'片段未到时间轴末尾，但读取提前结束');break
                    ctl.idx+=1
                    ctl.drawn=ctl._fit(frame)
                    ctl.pos_s=seg['start_s']+ctl.idx/fps
            ctl.paused=False


def save_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)
