import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_runner", ROOT / "scripts/smoke-runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class HostPreflightTests(unittest.TestCase):
    def test_root_host_is_refused_before_podman_access(self):
        with patch.object(runner.os, "geteuid", return_value=0), patch.object(runner, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                runner.require_rootless_host()
        command.assert_not_called()

    def test_rootful_podman_is_refused(self):
        with patch.object(runner.os, "geteuid", return_value=1000), patch.object(runner, "run", return_value="false") as command:
            with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                runner.require_rootless_host()
        command.assert_called_once_with("podman", "info", "--format", "{{.Host.Security.Rootless}}")

    def test_host_check_precedes_evidence_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            bundle.mkdir()
            for name in ("runner.iso", "machine.ign", "machine.qcow2"):
                (bundle / name).touch()
            evidence = Path(temp) / "evidence"
            with patch.object(runner, "run"), patch.object(runner, "require_rootless_host", side_effect=RuntimeError("rootless Podman")):
                with self.assertRaisesRegex(RuntimeError, "rootless Podman"):
                    runner.smoke(bundle, evidence)
            self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
