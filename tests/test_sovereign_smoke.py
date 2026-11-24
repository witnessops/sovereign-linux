import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bindings import sovereign_smoke as smoke


class OfflineSmoke(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "worker"
        self.cmd = dict(profile=smoke.PROFILE, device_id=smoke.DEVICE, operation="run",
                        request_id="smoke_" + "1" * 32, claim_id="claim_" + "2" * 32)
        self.preflight = Mock()
        self.start = Mock(side_effect=self.complete)
        self.folder = self.state / self.cmd["request_id"]

    def send(self, **fields):
        return smoke.handle({**self.cmd, **fields}, state=self.state,
                            preflight=self.preflight, start=self.start)

    def complete(self, folder):
        folder.mkdir(exist_ok=True)
        identity = {"workflow_id": "local-workflow", "execution_id": "1"}
        records = {
            "dispatch-intent": {"bundle": smoke.BUNDLE.name, "fixture_sha256": smoke.FIXTURE,
                                "runner_image": smoke.RUNNER, "main_image": smoke.MAIN,
                                "workflow_id": identity["workflow_id"]},
            "execution": identity,
            "stop": {"vm_stopped": True, "main_stopped": True, "qemu_exit": 0},
            "result": {**identity, "status": "success", "database_executions": 1,
                       "items": 1, "code_runs": 1, "marker": smoke.MARKER, "result": 42,
                       "readbacks": ["n8n execution API", "stopped main SQLite"],
                       "vm_stopped": True, "main_stopped": True, "qemu_exit": 0},
        }
        for name, record in records.items():
            (folder / (name + ".json")).write_text(json.dumps(record))

    def test_repeat_run_and_status_use_same_durable_receipt(self):
        first = self.send()
        self.assertEqual(("complete", "1", 42), (first["state"], first["execution_id"], first["result"]))
        self.assertTrue(first["vm_stopped"] and first["main_stopped"])
        # Prove terminal reads use the ledger even after a new process or missing evidence.
        (self.folder / "result.json").unlink()
        self.assertEqual(first, self.send(operation="status", execution_id="1"))
        self.assertEqual(first, self.send())
        self.start.assert_called_once()
        self.preflight.assert_called_once()

    def test_simultaneous_deliveries_only_dispatch_once(self):
        entered, release = threading.Event(), threading.Event()

        def slow(folder):
            with folder.with_suffix(".log").open("xb") as log:
                fcntl.flock(log, fcntl.LOCK_EX)
                entered.set()
                self.assertTrue(release.wait(10))
                self.complete(folder)

        self.start.side_effect = slow
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.send)
            try:
                self.assertTrue(entered.wait(10))
                reads = [pool.submit(self.send, operation=op) for op in ("run", "status", "run")]
                for item in reads:
                    self.assertEqual("running", item.result(5)["state"])
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    self.send(request_id="smoke_" + "3" * 32)
            finally:
                release.set()
            self.assertEqual("complete", first.result()["state"])
        self.start.assert_called_once()

    def test_uncertain_submission_is_never_retried(self):
        self.start.side_effect = OSError("unknown launch outcome")
        self.assertEqual("uncertain", self.send()["state"])
        self.assertEqual("uncertain", self.send()["state"])
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.send(request_id="smoke_" + "3" * 32)
        self.complete(self.folder)
        self.assertEqual("complete", self.send(operation="status")["state"])
        self.start.assert_called_once()

    def test_crash_after_reservation_survives_reopen(self):
        self.start.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        self.assertEqual("uncertain", self.send()["state"])
        self.start.assert_called_once()

    def test_unknown_status_has_no_side_effect(self):
        self.assertEqual("not_found", self.send(operation="status")["state"])
        self.assertFalse(self.state.exists())
        self.start.assert_not_called()

    def test_broker_tool_contract(self):
        args = {"request_id": self.cmd["request_id"]}
        trusted = {"device_id": smoke.DEVICE, "claim_id": self.cmd["claim_id"]}
        replies = []
        for tool in ("run_sovereign_linux_smoke", "run_sovereign_linux_smoke", "read_sovereign_linux_smoke"):
            command = smoke.broker_command(tool, args, **trusted)
            replies.append(smoke.handle(command, state=self.state, preflight=self.preflight, start=self.start))
        self.assertEqual(replies, [replies[0]] * 3)
        self.assertEqual(replies[0]["state"], "complete")
        for tool, extra in (("run_local_n8n_smoke", {}), ("run_sovereign_linux_smoke", trusted)):
            with self.assertRaises(ValueError):
                smoke.broker_command(tool, {**args, **extra}, **trusted)
        self.start.assert_called_once()

    def test_racing_first_deliveries(self):
        ready = threading.Barrier(6)

        def deliver(_):
            ready.wait(5)
            return self.send()

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(deliver, range(6)))
        self.assertTrue(any(r["state"] == "complete" for r in responses))
        self.assertEqual("complete", self.send(operation="status")["state"])
        self.start.assert_called_once()

    def test_unpolled_completed_worker_allows_next_request(self):
        self.start.side_effect = lambda path: None
        self.send()
        self.complete(self.folder)
        self.start.side_effect = self.complete
        second = self.send(request_id="smoke_"+"3"*32, claim_id="claim_"+"4"*32)
        self.assertEqual("complete", second["state"])
        self.assertEqual("complete", self.send()["state"])
        self.assertEqual(self.start.call_count, 2)

    def test_separate_fresh_databases_can_both_have_execution_one(self):
        first = self.send()
        second = self.send(request_id="smoke_"+"3"*32, claim_id="claim_"+"4"*32)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(self.start.call_count, 2)

    def test_wrong_claim_or_execution_refused(self):
        self.send()
        for fields in (dict(claim_id="claim_"+"4"*32), dict(operation="status", claim_id="claim_"+"4"*32),
                       dict(operation="status", execution_id="2")):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.send(**fields)
        self.start.assert_called_once()

    def test_untrusted_fields_refused_before_dispatch(self):
        for fields in (dict(profile="legacy"), dict(device_id="other"), dict(operation="shell"),
                       dict(path="/tmp/task"), dict(request_id="../request"), dict(execution_id="1")):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.send(**fields)
        self.start.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_pin_failure_prevents_reservation(self):
        self.preflight.side_effect = ValueError("Changed bundle manifest")
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.send()
        self.assertEqual("not_found", self.send(operation="status")["state"])
        self.start.assert_not_called()

    def test_partial_or_mismatched_evidence_never_completes(self):
        self.start.side_effect = lambda folder: folder.mkdir()
        self.send()
        self.complete(self.folder)
        p = self.folder / "result.json"
        valid = json.loads(p.read_text())
        for value in ("{", json.dumps({**valid, "workflow_id": "other"}),
                      json.dumps({**valid, "result": 42.0}), json.dumps({**valid, "database_executions": True})):
            p.write_text(value)
            self.assertEqual("uncertain", self.send()["state"])
        p.write_text(json.dumps(valid))
        (self.folder / "stop.json").write_text('{"vm_stopped":false}')
        self.assertEqual("uncertain", self.send()["state"])
        self.complete(self.folder)
        self.assertEqual("complete", self.send()["state"])
        self.start.assert_called_once()

    def test_launch_command_uses_only_fixed_target(self):
        with patch.object(smoke.subprocess, "Popen") as spawn:
            smoke.launch(self.root / self.cmd["request_id"])
        argv = spawn.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "-I", str(smoke.REPO / "scripts/smoke-runner.py"),
                                str(smoke.BUNDLE), str(self.root / self.cmd["request_id"])])

    def test_stdin_status_command(self):
        command = dict(self.cmd, operation="status")
        result = subprocess.run([sys.executable, "-I", smoke.__file__], input=json.dumps(command),
                                text=True, capture_output=True, check=True, env={**os.environ, "HOME":str(self.root)})
        self.assertEqual("not_found", json.loads(result.stdout)["state"])
        self.assertFalse((self.root / ".local").exists())

    def test_real_child_log_lock_and_exit_without_vm(self):
        # Exercise stdout inheritance and detached startup with a harmless local process.
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "smoke-runner.py").write_text(
            "import pathlib, sys, time\n"
            "p = pathlib.Path(sys.argv[2])\np.mkdir()\n"
            "for _ in range(500):\n"
            "    if (p/'finish').exists(): break\n"
            "    time.sleep(.01)\n")
        child = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            child.append(popen(*args, **kwargs))
            return child[-1]

        self.start.side_effect = smoke.launch
        with patch.object(smoke, "REPO", self.root), patch.object(smoke.subprocess, "Popen", capture):
            try:
                self.assertEqual("running", self.send()["state"])
                self.assertEqual("running", self.send()["state"])
                until = time.monotonic() + 3
                while not self.folder.exists() and time.monotonic() < until:
                    time.sleep(.01)
                (self.folder / "finish").touch()
                child[0].wait(5)
                self.assertEqual("uncertain", self.send()["state"])
                self.start.assert_called_once()
            finally:
                for proc in child:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
