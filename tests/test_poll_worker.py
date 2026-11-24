from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
worker = runpy.run_path(str(ROOT / 'bindings/poll_worker.py'))
binding = runpy.run_path(str(ROOT / 'bindings/sovereign_smoke.py'))


class WorkerTests(unittest.TestCase):
    def test_idle_poll_performs_only_local_read(self):
        handle = Mock(return_value={'state': 'not_found'})
        post = Mock(return_value={'command':None,'probe':'not_found'})
        self.assertFalse(worker['tick']({**binding,'handle':handle},post))
        self.assertEqual(handle.call_args.args[0]['operation'],'status')
        self.assertEqual(post.call_args.args[1]['revision'],binding['REVISION'])

    def test_unseen_claim_only_polled_for_status_never_starts(self):
        cmd = binding['broker_command']('read_sovereign_linux_smoke',{'request_id':'smoke_'+'a'*32},
                                        device_id=worker['DEVICE'],claim_id='claim_'+'b'*32)
        handle = Mock(side_effect=[{'state':'not_found'},{'state':'not_found'}])
        post = Mock(side_effect=[{'command':cmd,'probe':'not_found'},{'accepted': True}])
        self.assertTrue(worker['tick']({**binding,'handle':handle},post))
        self.assertEqual([c.args[0]['operation'] for c in handle.call_args_list], ['status','status'])
        self.assertEqual(post.call_args.args,('report',{'state':'uncertain'}))
