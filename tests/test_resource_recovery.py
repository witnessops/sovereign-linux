"""Exercise interrupted startup and recovery with a disk-backed Podman stand-in."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, SOURCE / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("binding", "bindings/sovereign_smoke.py")
runner = load("runner", "scripts/smoke-runner.py")
STORE = "1" * 64
NETWORK_ID = "a" * 64


def info_for(graph_root):
    return json.dumps({"host": {"security": {"rootless": True}, "serviceIsRemote": False,
                                "networkBackend": "netavark"},
                       "store": {"graphRoot": str(graph_root), "graphDriverName": "overlay",
                                 "runRoot": "/run/user/1000/containers"}})


def model_child(case, root, evidence):
    """Only the child owns this fake engine. Any unexpected command fails."""
    engine = root / "engine"

    def command(*args, **kwargs):
        if args == ("sha256sum", "-c", "SHA256SUMS"):
            return ""
        if args[:3] == ("podman", "network", "create"):
            assert json.loads((evidence / "resource-intent.json").read_text())["name"] == args[-1]
            (engine / "network").write_text(json.dumps(args[3:]))
            return args[-1]  # Podman network create prints the name.
        if args[:3] == ("podman", "network", "exists"):
            return ""
        if args[:3] == ("podman", "network", "inspect"):
            created = json.loads((engine / "network").read_text())
            labels = dict(created[i + 1].split("=", 1) for i, arg in enumerate(created) if arg == "--label")
            return f"{created[-1]} {NETWORK_ID} {json.dumps(labels)}"
        if args[:2] == ("podman", "create"):
            (engine / "container").write_text(json.dumps(args[2:]))
            if case == "kill":
                os.kill(os.getpid(), signal.SIGKILL)
            raise subprocess.TimeoutExpired(["podman", "create"], 90)
        if args[:3] == ("podman", "network", "rm"):
            assert args[-1] == NETWORK_ID
            (engine / "network-rm-attempt").touch()
            if (engine / "container").exists():
                raise subprocess.CalledProcessError(125, args, stderr="network in use")
        raise AssertionError(f"Unexpected command: {args[:3]}")

    if case == "lost-ack":
        # Simulate the n8n side accepting one trigger before the execution ID returns.
        evidence.mkdir()
        runner.record(evidence / "dispatch-intent.json", {
            "bundle": worker.BUNDLE.name, "fixture_sha256": worker.FIXTURE,
            "main_image": worker.MAIN, "runner_image": worker.RUNNER, "workflow_id": "synthetic",
        })
        with (engine / "triggers").open("a") as stream:
            stream.write("accepted\n")
        os.kill(os.getpid(), signal.SIGKILL)

    with patch.object(runner, "REPO", root), patch.object(runner, "run", side_effect=command), \
            patch.object(runner, "rootless_store_id", return_value=STORE):
        try:
            runner.smoke(root / "bundle", evidence)
        except subprocess.CalledProcessError as error:
            if (case == "timeout" and isinstance(error.__context__, subprocess.TimeoutExpired)
                    and error.cmd[:3] == ("podman", "network", "rm")):
                return 75
            raise
    raise AssertionError("Interruption did not occur")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("engine", "runner", "build", "bundle", "tmp"):
            (self.root / name).mkdir()
        shutil.copyfile(SOURCE / "runner/smoke-workflow.json", self.root / "runner/smoke-workflow.json")
        for name in ("runner.iso", "machine.ign", "machine.qcow2"):
            (self.root / "bundle" / name).touch()
        self.state = self.root / "worker"
        self.request = "smoke_" + "1" * 32
        self.claim = "claim_" + "2" * 32
        self.evidence = self.state / self.request

    def interrupt_then_redeliver(self, case):
        engine = self.root / "engine"
        foreign = engine / "other-request"
        foreign.write_text("foreign container and network")
        def start(evidence):
            with evidence.with_suffix(".log").open("xb") as log:
                child = subprocess.run([sys.executable, "-I", "-B", __file__, "--child",
                                        case, str(self.root), str(evidence)], stdout=log,
                                       stderr=subprocess.STDOUT, timeout=10,
                                       env={**os.environ, "TMPDIR": str(self.root / "tmp")})
            self.assertEqual(child.returncode, 75 if case == "timeout" else -signal.SIGKILL,
                             evidence.with_suffix(".log").read_text())

        launch = Mock(side_effect=start)
        def send(operation, request=self.request, claim=self.claim):
            frame = dict(operation=operation, request_id=request, claim_id=claim,
                         device_id=worker.DEVICE, profile=worker.PROFILE)
            return worker.handle(frame, state=self.state, preflight=lambda: None, start=launch)

        self.assertEqual(send("run")["state"], "uncertain")
        resources = {p.name: p.read_bytes() for p in engine.iterdir()}
        for op in ("status", "run", "run", "status"):
            self.assertEqual(send(op)["state"], "uncertain")
        with self.assertRaisesRegex(ValueError, "needs reconciliation"):
            send("run", "smoke_" + "3" * 32, "claim_" + "4" * 32)
        launch.assert_called_once()
        self.assertEqual(resources, {p.name: p.read_bytes() for p in engine.iterdir()})
        self.assertEqual(foreign.read_text(), "foreign container and network")
        return resources

    def check_interrupted_create(self, case):
        resources = self.interrupt_then_redeliver(case)
        intent = json.loads((self.evidence / "resource-intent.json").read_text())
        self.assertEqual(intent["request_id"], self.request)
        self.assertNotIn("triggers", resources)
        self.assertEqual("network-rm-attempt" in resources, case == "timeout")
        if case == "timeout":
            self.assertEqual(json.loads((self.evidence / "stop.json").read_text()), {})
        else:
            self.assertFalse((self.evidence / "stop.json").exists())

        inventory = {}
        for kind, resource_id in (("network", "a" * 64), ("container", "b" * 64)):
            args = json.loads(resources[kind])
            name = args[-1] if kind == "network" else args[args.index("--name") + 1]
            labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
            self.assertEqual(labels, {runner.REQUEST_LABEL: self.request, runner.ATTEMPT_LABEL: intent["attempt"]})
            self.assertEqual(name, intent["name"])
            inventory[kind] = (name, resource_id, labels)

        def read(*args, **kwargs):
            binary, kind, action, *fields, name = args
            self.assertEqual((binary, name), ("podman", inventory[kind][0]))
            if action == "exists":
                self.assertEqual(fields, [])
                return ""
            label_field = ".Config.Labels" if kind == "container" else ".Labels"
            self.assertEqual((action, fields), ("inspect", ["--format", "{{.Name}} {{.ID}} {{json " + label_field + "}}"]))
            _, resource_id, labels = inventory[kind]
            return f"{name} {resource_id} {json.dumps(labels)}"

        def snapshot():
            return runner.inspect_resources(self.evidence)["resources"]

        with patch.object(runner, "rootless_store_id", return_value=STORE), patch.object(runner, "run", side_effect=read):
            expected = {k: {"state": "owned", "id": item[1]} for k, item in inventory.items()}
            self.assertEqual(snapshot(), expected)
            self.assertEqual(snapshot(), expected)
            # A replacement can reuse the exact generated name and one of the labels.
            inventory["container"][2][runner.ATTEMPT_LABEL] = "wrong-attempt"
            self.assertEqual(snapshot(), {**expected, "container": {"state": "foreign"}})
            inventory["network"][2][runner.REQUEST_LABEL] = "another-request"
            self.assertEqual(snapshot(), {kind: {"state": "foreign"} for kind in inventory})

    def test_legacy_partial_and_swapped_intent_do_not_query_podman(self):
        self.evidence.mkdir(parents=True)
        intent = self.evidence / "resource-intent.json"
        with patch.object(runner, "run") as command, patch.object(runner, "rootless_store_id", return_value=STORE) as preflight:
            self.assertEqual(runner.inspect_resources(self.evidence)["reason"], "missing_intent")
            for contents in ("{", json.dumps({"name": "--all", "request_id": self.request, "attempt": "a"*32}),
                             json.dumps({"name": "sovereign-smoke-" + "a"*12, "request_id": "other", "attempt": "a"*32})):
                intent.write_text(contents)
                self.assertEqual(runner.inspect_resources(self.evidence)["reason"], "invalid_intent")
            command.assert_not_called()
            preflight.assert_not_called()

    def test_unwind_leaves_same_name_replacement_intact(self):
        inventory = {}
        removal_targets = []
        foreign_id = "b" * 64

        def command(*args, **kwargs):
            if args[0] == "sha256sum":
                return ""
            if args[:3] == ("podman", "network", "create"):
                name = args[-1]
                labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
                inventory[NETWORK_ID] = (name, labels)
                return name
            if args[:3] == ("podman", "network", "exists"):
                return ""
            if args[:3] == ("podman", "network", "inspect"):
                name, labels = inventory[NETWORK_ID]
                self.assertEqual(args[-1], name)
                return f"{name} {NETWORK_ID} {json.dumps(labels)}"
            if args[:2] == ("podman", "create"):
                name, _ = inventory.pop(NETWORK_ID)
                inventory[foreign_id] = (name, {})
                self.assertEqual(args[args.index("--network") + 1], NETWORK_ID)
                raise subprocess.TimeoutExpired("simulated create", 90)
            if args[:3] == ("podman", "network", "rm"):
                target = args[-1]
                removal_targets.append(target)
                for resource_id, (name, _) in list(inventory.items()):
                    if target in (name, resource_id):
                        del inventory[resource_id]
                        return ""
                raise subprocess.CalledProcessError(1, args)
            self.fail(f"Unexpected command: {args[:3]}")

        with patch.object(runner, "REPO", self.root), patch.object(runner, "rootless_store_id", return_value=STORE), \
                patch.object(runner, "run", side_effect=command), self.assertRaises(subprocess.TimeoutExpired):
            runner.smoke(self.root / "bundle", self.evidence)
        self.assertEqual(removal_targets, [NETWORK_ID])
        self.assertEqual(list(inventory), [foreign_id])

    def test_unverified_network_never_arms_cleanup_or_starts_container(self):
        for state in ("foreign", "unknown", "absent"):
            evidence = self.root / state
            with self.subTest(state=state), patch.object(runner, "REPO", self.root), \
                    patch.object(runner, "rootless_store_id", return_value=STORE), \
                    patch.object(runner, "inspect_resource", return_value={"state": state}), \
                    patch.object(runner, "run", return_value="") as command:
                with self.assertRaisesRegex(RuntimeError, "verify"):
                    runner.smoke(self.root / "bundle", evidence)
                self.assertEqual([c.args[:3] for c in command.call_args_list],
                                 [("sha256sum", "-c", "SHA256SUMS"), ("podman", "network", "create")])

    def test_different_rootless_store_is_unknown_before_resource_lookup(self):
        self.evidence.mkdir(parents=True)
        graph_root = self.root / "original-storage"
        graph_root.mkdir()
        other_root = self.root / "empty-storage"
        other_root.mkdir()
        with patch.object(runner.os, "geteuid", return_value=1000), \
                patch.object(runner, "run", return_value=info_for(graph_root)) as command:
            runner.save_resource_intent(self.evidence, "sovereign-smoke-" + "a"*12, runner.rootless_store_id())
            for path in (other_root, graph_root):
                if path == graph_root:
                    graph_root.rename(self.root / "retired-test-directory")
                    graph_root.mkdir()  # Same configured path, different store directory.
                command.reset_mock()
                command.return_value = info_for(path)
                self.assertEqual(runner.inspect_resources(self.evidence),
                                 {"request_id": self.request, "state": "unknown", "reason": "store_mismatch"})
                command.assert_called_once_with("podman", "info", "--format", "json", timeout=10)

    def test_store_change_during_inspection_discards_snapshot(self):
        self.evidence.mkdir(parents=True)
        runner.save_resource_intent(self.evidence, "sovereign-smoke-" + "a"*12, STORE)
        with patch.object(runner, "rootless_store_id", side_effect=[STORE, "2"*64]), \
                patch.object(runner, "inspect_resource", return_value={"state": "absent"}):
            self.assertEqual(runner.inspect_resources(self.evidence),
                             {"request_id": self.request, "state": "unknown", "reason": "store_mismatch"})

    def test_intent_without_store_id_is_unknown_before_any_podman_call(self):
        self.evidence.mkdir(parents=True)
        runner.record(self.evidence / "resource-intent.json",
                      {"name": "sovereign-smoke-" + "a"*12, "attempt": "a"*32, "request_id": self.request})
        with patch.object(runner, "run") as command:
            self.assertEqual(runner.inspect_resources(self.evidence)["reason"], "unverified_store")
            command.assert_not_called()

    def test_absence_and_lookup_failure_are_distinct(self):
        self.evidence.mkdir(parents=True)
        runner.save_resource_intent(self.evidence, "sovereign-smoke-" + "a"*12, STORE)
        missing = subprocess.CalledProcessError(1, "exists")
        denied = subprocess.CalledProcessError(125, "exists")
        raced = subprocess.CalledProcessError(1, "inspect")
        for replies, expected in (([missing, denied], ("absent", "unknown")),
                                  (["", raced, missing], ("unknown", "absent")),
                                  (["", "malformed", missing], ("unknown", "absent"))):
            with self.subTest(expected=expected), patch.object(runner, "rootless_store_id", return_value=STORE), \
                    patch.object(runner, "run", side_effect=replies):
                resources = runner.inspect_resources(self.evidence)["resources"]
                self.assertEqual((resources["container"]["state"], resources["network"]["state"]), expected)

    def test_standalone_inspection_cli_reads_only_and_accepts_custom_directory(self):
        evidence = self.root / "standalone smoke"
        evidence.mkdir()
        info = info_for(self.root)
        with patch.object(runner, "run", return_value=info), patch.object(runner.os, "geteuid", return_value=os.geteuid() or 1000):
            store_id = runner.rootless_store_id()
        runner.save_resource_intent(evidence, "sovereign-smoke-" + "a"*12, store_id)
        cmd = [sys.executable, "-I", "-B", str(SOURCE / "scripts/smoke-runner.py"),
               "--inspect-resources", str(evidence)]
        tool = self.root / "podman"
        tool.write_text("#!/bin/sh\n[ \"$1\" = --remote=false ] || exit 125\nshift\ncase \"$1 $2\" in\n"
                        f"'info --format') echo '{info}';;\n"
                        "'container exists'|'network exists') exit 1;;\n*) exit 125;;\nesac\n")
        tool.chmod(0o700)
        result = subprocess.run(cmd, env={**os.environ, "PATH": str(self.root)},
                                capture_output=True, text=True)
        if os.geteuid() == 0:
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["reason"], "rootless_host_unavailable")
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["resources"],
                             {kind: {"state": "absent"} for kind in ("container", "network")})
        self.assertEqual(sorted(p.name for p in evidence.iterdir()), ["resource-intent.json"])

    def test_sigkill_after_create_before_id_return(self):
        self.check_interrupted_create("kill")

    def test_create_timeout_after_engine_side_effect(self):
        self.check_interrupted_create("timeout")

    def test_lost_dispatch_ack_preserves_one_simulated_trigger(self):
        resources = self.interrupt_then_redeliver("lost-ack")
        self.assertEqual(resources["triggers"], b"accepted\n")
        self.assertFalse((self.evidence / "execution.json").exists())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        sys.exit(model_child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])))
    unittest.main(verbosity=2)
