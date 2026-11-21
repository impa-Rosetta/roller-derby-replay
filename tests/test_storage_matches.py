import json
from pathlib import Path
import tempfile
import unittest

from storage_matches import build_matches


NODES = ['rk1','rk2','rk3','rk4']


def row(node, name='stage1_20261121_120059', location='rk', start=1000):
    return dict(node=node,name=name,location=location,start=start,seconds=3900,bytes=100,
                verified=location=='dgx',busy=False,id=node+'|'+location+'|'+name,
                directory='/record/'+name,segments=[])


class MatchGroupingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'matches').mkdir()

    def tearDown(self):self.temp.cleanup()

    def save(self, name, rows):
        (self.root/'matches'/(name+'.json')).write_text(json.dumps(dict(name=name,
                    tracks={r['node']:dict(name=r['name'],offset=1.5,calibrated=True) for r in rows})))

    def test_four_cameras_with_different_folder_names_are_one_explicit_match(self):
        rows=[row(n, 'stage1_20261121_120056' if n=='rk4' else 'stage1_20261121_120059') for n in NODES]
        self.save('Hour test',rows)
        matches=build_matches(rows,self.root,NODES)
        self.assertEqual(len(matches),1);self.assertEqual(matches[0]['camera_count'],4)
        self.assertEqual(matches[0]['rk_bytes'],400)
        self.assertTrue(matches[0]['track_settings']['rk1']['calibrated'])

    def test_copies_and_duplicate_preview_manifests_do_not_create_extra_matches(self):
        rows=[row(n) for n in NODES];self.save('Match',rows)
        self.save('Single camera preview',[rows[0]])
        rows += [row(n,location='dgx') for n in NODES]
        matches=build_matches(rows,self.root,NODES)
        self.assertEqual(len(matches),1);self.assertEqual(len(matches[0]['members']),8)
        self.assertEqual(matches[0]['verified_count'],4)

    def test_nearby_unrelated_recordings_are_not_guessed_into_a_match(self):
        rows=[row(n,start=1000+i*.1) for i,n in enumerate(NODES)]
        matches=build_matches(rows,self.root,NODES)
        self.assertEqual(len(matches),4)
        self.assertTrue(all(m['camera_count']==1 and m['expected']==4 for m in matches))

    def test_missing_camera_is_reported_incomplete_and_dgx_only_match_survives(self):
        rows=[row(n) for n in NODES];self.save('Match',rows)
        matches=build_matches([row(n,location='dgx') for n in NODES[:3]],self.root,NODES)
        self.assertEqual(len(matches),1);self.assertEqual(matches[0]['camera_count'],3)
        self.assertEqual(matches[0]['expected'],4);self.assertEqual(matches[0]['rk_count'],0)

    def test_distinct_adjacent_matches_stay_separate(self):
        first=[row(n) for n in NODES]
        second=[row(n,name='stage1_20261121_120110',start=1011) for n in NODES]
        self.save('First',first);self.save('Second',second)
        matches=build_matches(first+second,self.root,NODES)
        self.assertEqual(len(matches),2)
        self.assertTrue(all(m['camera_count']==4 for m in matches))


if __name__=='__main__':unittest.main()
