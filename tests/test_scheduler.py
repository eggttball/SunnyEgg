import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from scheduler import next_trigger, load_schedule, trigger_once, main

class SchedulerTests(unittest.TestCase):
    def test_taipei_twenty_before_at_after_and_utc_input(self):
        before = datetime.fromisoformat('2026-11-01T19:59:59+08:00')
        at = datetime.fromisoformat('2026-11-01T20:00:00+08:00')
        after = datetime.fromisoformat('2026-11-01T20:00:01+08:00')
        self.assertEqual(next_trigger(before).isoformat(), '2026-11-01T20:00:00+08:00')
        self.assertEqual(next_trigger(at), at)
        self.assertEqual(next_trigger(after).isoformat(), '2026-11-02T20:00:00+08:00')
        self.assertEqual(next_trigger(before.astimezone(timezone.utc)), at)

    def test_naive_clock_rejected(self):
        with self.assertRaises(ValueError):
            next_trigger(datetime(2026,11,1,19,0))

    def test_repository_schedule_has_one_authority_and_photo_only(self):
        root = Path(__file__).resolve().parents[1]
        config = load_schedule(root)
        self.assertEqual(config['schedule'], {'cron':'0 20 * * *', 'timeZone':'Asia/Taipei'})
        self.assertEqual(config['media'], 'photos')

    def test_legacy_continuous_scheduler_does_not_run_without_consent(self):
        with patch('scheduler.load_schedule', return_value={}), patch('scheduler.run_daily') as batch, patch('sys.stderr'):
            self.assertEqual(main([]), 2)
            batch.assert_not_called()

    def test_once_invokes_batch_with_explicit_replay_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('scheduler.load_schedule', return_value={'threshold':0.5}), patch('scheduler.run_daily', return_value={'state':'complete','simulated':True}) as batch:
                result = trigger_once(root, discovery_file=root/'observed.json', simulate=True, env_file=root/'.env')
                self.assertEqual(result['state'], 'complete')
                self.assertTrue(batch.call_args.kwargs['simulate'])
                self.assertEqual(batch.call_args.kwargs['discovery_file'], root/'observed.json')

if __name__ == '__main__':
    unittest.main()
