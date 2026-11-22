"""A stoppable sports clock mapped to DGX wall time, independent of playback.

Video keeps its full recording axis. Paused match time maps to an interval of
video, never to an ambiguous single seek position. Persist only transitions.
"""
from bisect import bisect_right
import math
import time


def mmss(seconds):
    value=max(0,int(abs(seconds)+1e-6))
    return '%02d:%02d'%(value//60,value%60)


class MatchClock:
    def __init__(self,data=None,wall=time.time,monotonic=time.monotonic):
        self.wall=wall;self.monotonic=monotonic;self.transitions=[];self.ended_wall=None
        if data:
            if data.get('version')!=1:raise ValueError('Unsupported match clock version')
            for item in data.get('transitions',[]):
                w=float(item['wall']);elapsed=float(item['elapsed']);running=item['running']
                if not math.isfinite(w) or not math.isfinite(elapsed) or elapsed<0 or type(running) is not bool:
                    raise ValueError('Invalid match clock transition')
                if self.transitions and (w<self.transitions[-1]['wall'] or elapsed<self.transitions[-1]['elapsed']):
                    raise ValueError('Match clock transitions out of order')
                self.transitions.append(dict(wall=w,elapsed=elapsed,running=running))
            if self.transitions and (self.transitions[0]['elapsed']!=0 or not self.transitions[0]['running']):
                raise ValueError('Match clock must start running at zero')
            if data.get('ended_wall') is not None:
                self.ended_wall=float(data['ended_wall'])
                if (not math.isfinite(self.ended_wall) or not self.transitions or
                    self.transitions[-1]['running'] or self.ended_wall!=self.transitions[-1]['wall']):
                    raise ValueError('Invalid match end time')
        self._walls=[t['wall'] for t in self.transitions]
        self._mono_anchor=monotonic()
        self._elapsed_anchor=max(0,self.at(wall())[0]) if self.started else 0.

    @property
    def started(self):return bool(self.transitions)

    @property
    def running(self):return self.started and not self.ended and self.transitions[-1]['running']

    @property
    def ended(self):return self.ended_wall is not None

    def current(self):
        return self._elapsed_anchor+max(0,self.monotonic()-self._mono_anchor) if self.running else self._elapsed_anchor

    def at(self,wall):
        if not self.started:return 0.,'unstarted'
        if self.ended and wall>=self.ended_wall:return self.transitions[-1]['elapsed'],'ended'
        i=bisect_right(self._walls,wall)-1
        if i<0:return wall-self.transitions[0]['wall'],'before'
        item=self.transitions[i]
        if not item['running']:return item['elapsed'],'paused'
        return item['elapsed']+max(0,wall-item['wall']),'running'

    def label_at(self,wall):
        seconds,phase=self.at(wall)
        if phase=='unstarted':return '未开始'
        return ('赛前 ' if phase=='before' else '暂停 ' if phase=='paused' else '赛后 ' if phase=='ended' else '')+mmss(seconds)

    def start(self):
        if self.started:return False
        self._append(True,0.);return True

    def pause(self):
        if not self.running:return False
        self._append(False,self.current());return True

    def resume(self):
        if not self.started or self.running or self.ended:return False
        self._append(True,self.current());return True

    def finish(self):
        if not self.started or self.ended:return False
        self._append(False,self.current());self.ended_wall=self.transitions[-1]['wall'];return True

    def _append(self,running,elapsed):
        wall=max(self.wall(),self._walls[-1] if self._walls else -math.inf)
        self.transitions.append(dict(wall=wall,elapsed=elapsed,running=running));self._walls.append(wall)
        self._elapsed_anchor=elapsed;self._mono_anchor=self.monotonic()

    def dump(self):
        return dict(version=1,transitions=[dict(t) for t in self.transitions],ended_wall=self.ended_wall)

    def event(self,index,origin=0,note=None):
        item=self.transitions[index]
        kind=('match_end' if self.ended and index==len(self.transitions)-1 else
              'match_start' if index==0 else 'match_resume' if item['running'] else 'match_pause')
        names=dict(match_start='比赛开始',match_pause='比赛暂停',match_resume='比赛继续',match_end='比赛结束')
        return dict(kind=kind,clock_transition=index,wall=item['wall'],match_elapsed=item['elapsed'],
                    position=max(0,item['wall']-origin) if origin else 0,note=note or names[kind])

    def paused_intervals(self,end):
        end=min(end,self.ended_wall) if self.ended else end
        return [(t['wall'],min(end,self.transitions[i+1]['wall']) if i+1<len(self.transitions) else end)
                for i,t in enumerate(self.transitions) if not t['running'] and t['wall']<end]
