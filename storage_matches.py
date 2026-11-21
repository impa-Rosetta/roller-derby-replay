"""Group physical video copies by the recorder's explicit match manifests.

Nearby recording times are deliberately insufficient evidence of membership.
Recordings without a saved match remain separate, visibly incomplete matches.
"""
import hashlib
import json
from pathlib import Path


def build_matches(rows, state_root, node_ids):
    slots = {}
    for row in rows:
        slots.setdefault((row['node'],row['name']),[]).append(row)
    candidates = []
    for p in (Path(state_root)/'matches').glob('*.json'):
        try:
            saved = json.loads(p.read_text())
            signature = frozenset((node,track['name']) for node,track in saved['tracks'].items()
                                  if node in node_ids)
            available = signature.intersection(slots)
            if available:
                candidates.append((len(available),len(signature),p.stat().st_mtime_ns,
                                   signature,saved))
        except (OSError,ValueError,KeyError,TypeError):
            continue
    assigned, groups = set(), []

    def add(signature,saved=None):
        available = signature.intersection(slots)
        members = [row for slot in sorted(available) for row in slots[slot]]
        if not members:return
        name = saved.get('name') if saved else None
        key = hashlib.sha256(json.dumps(sorted(signature)).encode()).hexdigest()[:16]
        start = min((r['start'] for r in members if r['start']),default=0)
        groups.append(dict(id='game_'+key,name=name or '未命名比赛 · '+members[0]['name'][7:],
                    start=start,seconds=max(r['seconds'] for r in members),members=members,
                    camera_count=len({r['node'] for r in members}),expected=len(node_ids),
                    rk_count=len({r['node'] for r in members if r['location']=='rk'}),
                    dgx_count=len({r['node'] for r in members if r['location']=='dgx'}),
                    verified_count=len({r['node'] for r in members if r['location']=='dgx' and r['verified']}),
                    rk_bytes=sum(r['bytes'] for r in members if r['location']=='rk'),
                    dgx_bytes=sum(r['bytes'] for r in members if r['location']=='dgx'),
                    bytes=sum(r['bytes'] for r in members),busy=any(r['busy'] for r in members),
                    track_settings=saved.get('tracks',{}) if saved else {}))
        assigned.update(available)

    # A complete four-camera manifest wins over duplicate single-camera views.
    for _,__,___,signature,saved in sorted(candidates,key=lambda c:c[:3],reverse=True):
        available = signature.intersection(slots)
        if available.intersection(assigned):continue
        add(signature,saved)
    for slot in sorted(slots):
        if slot not in assigned:add(frozenset([slot]))
    return sorted(groups,key=lambda g:g['start'],reverse=True)
