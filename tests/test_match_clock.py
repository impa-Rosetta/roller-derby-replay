import json
import unittest
from match_clock import MatchClock, mmss


class MatchClockTests(unittest.TestCase):
    def setUp(self):
        self.wall=1000.;self.mono=100.
        self.clock=MatchClock(wall=lambda:self.wall,monotonic=lambda:self.mono)

    def advance(self,seconds):self.wall+=seconds;self.mono+=seconds

    def test_start_from_zero_and_before_match_video_is_retained(self):
        self.assertFalse(self.clock.started);self.assertFalse(self.clock.pause())
        self.clock.start();self.assertEqual(self.clock.current(),0)
        self.assertEqual(self.clock.label_at(990),'赛前 00:10')
        self.assertEqual(self.clock.label_at(1000),'00:00')
        self.advance(65);self.assertEqual(self.clock.current(),65)
        self.assertEqual(mmss(self.clock.current()),'01:05')
        self.assertFalse(self.clock.start());self.assertEqual(self.clock.current(),65)

    def test_pause_freezes_match_clock_but_each_video_instant_remains_addressable(self):
        self.clock.start();self.advance(20);self.clock.pause();self.advance(10)
        self.assertEqual(self.clock.current(),20)
        self.assertEqual(self.clock.label_at(1022),'暂停 00:20')
        self.assertEqual(self.clock.label_at(1028),'暂停 00:20')
        self.assertEqual(self.clock.paused_intervals(1030),[(1020,1030)])
        self.clock.resume();self.advance(5)
        self.assertEqual(self.clock.current(),25)
        self.assertEqual(self.clock.label_at(1033),'00:23')
        self.assertEqual(self.clock.label_at(1030),'00:20')

    def test_multiple_pause_intervals_are_subtracted_from_match_time(self):
        self.clock.start();self.advance(10);self.clock.pause();self.advance(5);self.clock.resume()
        self.advance(20);self.clock.pause();self.advance(15);self.clock.resume();self.advance(7)
        self.assertEqual(self.clock.current(),37)
        self.assertEqual(self.clock.at(self.wall),(37,'running'))
        self.assertEqual(self.clock.paused_intervals(self.wall),[(1010,1015),(1035,1050)])

    def test_restoring_running_clock_includes_time_while_ui_was_closed(self):
        self.clock.start();self.advance(10);data=json.loads(json.dumps(self.clock.dump()))
        self.advance(30)
        restored=MatchClock(data,wall=lambda:self.wall,monotonic=lambda:self.mono)
        self.assertEqual(restored.current(),40)
        self.advance(2);self.assertEqual(restored.current(),42)

    def test_restoring_paused_clock_keeps_it_frozen(self):
        self.clock.start();self.advance(12);self.clock.pause();self.advance(600)
        restored=MatchClock(self.clock.dump(),wall=lambda:self.wall,monotonic=lambda:self.mono)
        self.assertFalse(restored.running);self.assertEqual(restored.current(),12)

    def test_live_counter_uses_monotonic_time_and_not_wall_clock_adjustments(self):
        self.clock.start();self.advance(10);self.wall+=3600
        self.assertEqual(self.clock.current(),10)

    def test_dump_does_not_share_mutable_transition_records(self):
        self.clock.start();data=self.clock.dump();data['transitions'][0]['elapsed']=999
        self.assertEqual(self.clock.current(),0)

    def test_finish_freezes_and_persists_terminal_state_with_postmatch_footage(self):
        self.clock.start();self.advance(65);self.assertTrue(self.clock.finish());self.advance(100)
        self.assertTrue(self.clock.ended);self.assertFalse(self.clock.running)
        self.assertEqual(self.clock.current(),65);self.assertEqual(self.clock.label_at(self.wall),'赛后 01:05')
        self.assertEqual(self.clock.label_at(1030),'00:30')
        self.assertFalse(self.clock.resume());self.assertFalse(self.clock.finish());self.assertFalse(self.clock.start())
        restored=MatchClock(self.clock.dump(),wall=lambda:self.wall,monotonic=lambda:self.mono)
        self.assertTrue(restored.ended);self.assertEqual(restored.current(),65)
        self.assertEqual(restored.paused_intervals(self.wall),[])

    def test_finish_while_paused_preserves_elapsed_and_closes_pause_interval(self):
        self.assertFalse(self.clock.finish());self.clock.start();self.advance(20);self.clock.pause()
        self.advance(10);self.clock.finish();self.advance(10)
        self.assertEqual(self.clock.current(),20);self.assertEqual(self.clock.paused_intervals(self.wall),[(1020,1030)])
        events=[self.clock.event(i,990) for i in range(len(self.clock.transitions))]
        self.assertEqual([e['kind'] for e in events],['match_start','match_pause','match_end'])
        self.assertEqual(events[-1]['position'],40);self.assertEqual(events[-1]['match_elapsed'],20)

    def test_malformed_or_out_of_order_clock_is_rejected(self):
        for transitions in [[dict(wall=float('nan'),elapsed=0,running=True)],
                            [dict(wall=1000,elapsed=0,running=False)],
                            [dict(wall=1000,elapsed=0,running=True),dict(wall=999,elapsed=1,running=False)]]:
            with self.assertRaises(ValueError):MatchClock(dict(version=1,transitions=transitions))


if __name__=='__main__':unittest.main()
