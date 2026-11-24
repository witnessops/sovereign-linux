import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_runner", ROOT / "scripts/smoke-runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


STORE = runner.PodmanStore("1" * 64, Path("/test/networks"))
CONTAINER_ID = "c" * 64


class HostPreflightTests(unittest.TestCase):
    def test_root_host_is_refused_before_podman_access(self):
        with patch.object(runner.os, "geteuid", return_value=0), patch.object(runner, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                runner.rootless_store()
        command.assert_not_called()

    def test_rootful_podman_is_refused(self):
        with patch.object(runner.os, "geteuid", return_value=1000), patch.object(runner, "run", return_value=json.dumps({
                "host": {"security": {"rootless": False}}, "store": {}})) as command:
            with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                runner.rootless_store()
        command.assert_called_once_with("podman", "info", "--format", "json", timeout=10)

    def test_host_check_precedes_evidence_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            bundle.mkdir()
            for name in ("runner.iso", "machine.ign", "machine.qcow2"):
                (bundle / name).touch()
            evidence = Path(temp) / "evidence"
            with patch.object(runner, "run"), patch.object(runner, "rootless_store", side_effect=RuntimeError("rootless Podman")):
                with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                    runner.smoke(bundle, evidence)
            self.assertFalse(evidence.exists())


class MainApiTests(unittest.TestCase):
    def test_owner_setup_password_uses_stdin_and_cookie_is_reused(self):
        replies = [
            {"status": 200, "cookies": ["n8n-auth=session-value; HttpOnly; Path=/"],
             "body": json.dumps({"data": {"owner": True}})},
            {"status": 200, "cookies": [], "body": json.dumps({"data": {"workflows": []}})},
        ]
        with patch.object(runner, "run", side_effect=[json.dumps(reply) for reply in replies]) as command:
            api = runner.MainApi("main-container", STORE)
            self.assertEqual(api.request("/owner/setup", {"password": "private-password"}), {"owner": True})
            self.assertEqual(api.request("/workflows/"), {"workflows": []})

        first, second = command.call_args_list
        self.assertEqual(first.args[:5], ("podman", "exec", "-i", "main-container", "node"))
        self.assertNotIn("private-password", " ".join(map(str, first.args)))
        self.assertEqual(json.loads(first.kwargs["input"])["body"]["password"], "private-password")
        self.assertEqual(json.loads(second.kwargs["input"])["cookie"], "n8n-auth=session-value")

    def test_execution_not_found_remains_retryable(self):
        reply = {"status": 404, "cookies": [], "body": "missing"}
        with patch.object(runner, "run", return_value=json.dumps(reply)):
            with self.assertRaises(HTTPError) as error:
                runner.MainApi("main-container", STORE).request("/executions/1")
        self.assertEqual(error.exception.code, 404)

    def test_main_ui_is_not_published_on_host(self):
        class StopAtCreate(Exception):
            pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runner").mkdir()
            (root / "build").mkdir()
            shutil.copyfile(ROOT / "runner/smoke-workflow.json", root / "runner/smoke-workflow.json")
            bundle = root / "bundle"
            bundle.mkdir()
            for name in ("runner.iso", "machine.ign", "machine.qcow2"):
                (bundle / name).touch()
            commands = []

            def command(*args, **kwargs):
                if args[:2] == ("podman", "create"):
                    commands.append(args)
                    raise StopAtCreate
                return ""

            with (
                patch.object(runner, "REPO", root),
                patch.object(runner, "rootless_store", return_value=STORE),
                patch.object(runner, "inspect_resource", return_value={"state": "owned", "id": "a" * 64}),
                patch.object(runner, "run", side_effect=command),
            ):
                with self.assertRaises(StopAtCreate):
                    runner.smoke(bundle, root / "evidence")

        self.assertEqual(len(commands), 1)
        self.assertIn("127.0.0.1::5679", commands[0])
        self.assertNotIn("127.0.0.1::5678", commands[0])


class TerminationTests(unittest.TestCase):
    def test_sigterm_unwinds_main_cleanup_and_records_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runner").mkdir()
            (root / "build").mkdir()
            shutil.copyfile(ROOT / "runner/smoke-workflow.json", root / "runner/smoke-workflow.json")
            bundle = root / "bundle"
            bundle.mkdir()
            for name in ("runner.iso", "machine.ign", "machine.qcow2"):
                (bundle / name).touch()
            evidence = root / "evidence"
            commands = []

            def command(*args, **kwargs):
                commands.append(args)
                if args[:2] == ("podman", "create"):
                    return CONTAINER_ID
                if args[:2] == ("podman", "port"):
                    runner.terminate_on_sigterm(runner.signal.SIGTERM, None)
                if args[:2] == ("podman", "inspect"):
                    return "false" if ("podman", "stop", "-t", "20", CONTAINER_ID) in commands else "true"
                return ""

            with (
                patch.object(runner, "REPO", root),
                patch.object(runner, "rootless_store", return_value=STORE),
                patch.object(runner, "inspect_resource", return_value={"state": "owned", "id": "a" * 64}),
                patch.object(runner, "run", side_effect=command),
                patch.object(runner.signal, "signal"),
            ):
                with self.assertRaises(SystemExit) as stopped:
                    runner.smoke(bundle, evidence)

            self.assertEqual(stopped.exception.code, 143)
            self.assertTrue(json.loads((evidence / "stop.json").read_text())["main_stopped"])
            self.assertIn(("podman", "stop", "-t", "20", CONTAINER_ID), commands)
            self.assertIn(("podman", "rm", "-v", CONTAINER_ID), commands)

    def test_sigterm_during_main_start_waits_for_cleanup_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runner").mkdir()
            (root / "build").mkdir()
            shutil.copyfile(ROOT / "runner/smoke-workflow.json", root / "runner/smoke-workflow.json")
            bundle = root / "bundle"
            bundle.mkdir()
            for name in ("runner.iso", "machine.ign", "machine.qcow2"):
                (bundle / name).touch()
            evidence = root / "evidence"
            commands = []

            def command(*args, **kwargs):
                commands.append(args)
                if args[:2] == ("podman", "create"):
                    return CONTAINER_ID
                if args[:2] == ("podman", "start"):
                    runner.signal.getsignal(runner.signal.SIGTERM)(runner.signal.SIGTERM, None)
                if args[:2] == ("podman", "inspect"):
                    return "false" if ("podman", "stop", "-t", "20", CONTAINER_ID) in commands else "true"
                return ""

            previous = runner.signal.getsignal(runner.signal.SIGTERM)
            runner.signal.signal(runner.signal.SIGTERM, runner.terminate_on_sigterm)
            try:
                with (
                    patch.object(runner, "REPO", root),
                    patch.object(runner, "rootless_store", return_value=STORE),
                    patch.object(runner, "inspect_resource", return_value={"state": "owned", "id": "a" * 64}),
                    patch.object(runner, "run", side_effect=command),
                ):
                    with self.assertRaises(SystemExit) as stopped:
                        runner.smoke(bundle, evidence)
            finally:
                runner.signal.signal(runner.signal.SIGTERM, previous)

            self.assertEqual(stopped.exception.code, 143)
            self.assertTrue(json.loads((evidence / "stop.json").read_text())["main_stopped"])
            self.assertIn(("podman", "stop", "-t", "20", CONTAINER_ID), commands)
            self.assertIn(("podman", "rm", "-v", CONTAINER_ID), commands)


if __name__ == "__main__":
    unittest.main()
