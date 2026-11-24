"""Offline tests plus pinned local Butane compilation when available; never call AWS."""
import base64
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aws_build", ROOT / "scripts/build-aws.py")
aws = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aws)


def config():
    return dict(aws.PIN, instance_type="t3.small", subnet_id="subnet-0123456789abcdef0",
                security_group_ids=["sg-0123456789abcdef0"], key_name="existing-owner-key",
                associate_public_ip_address=False, root_volume_gib=16,
                source_commit="7cd939f43a33c5fd244abdcf07a607168d285fe5")


def ignition():
    return {"ignition": {"version": "3.5.0"}, "storage": {"files": [{
        "path": "/etc/hostname", "mode": 420, "overwrite": True,
        "contents": {"compression": "", "source": "data:,sovereign-linux%0A"}}]},
        "systemd": {"units": [{"name": "sovereign-base-check.service", "enabled": True,
                                "contents": aws.BASE_UNIT}]}}


class ConfigurationTests(unittest.TestCase):
    def test_fixed_tuple_and_types_fail_closed(self):
        cases = {"release": "latest", "architecture": "aarch64", "region": "us-east-1",
                 "ami_id": "ami-00000000000000000", "expected_owner_id": "000000000000",
                 "root_device_name": "/dev/sda1", "instance_type": "t3.2xlarge",
                 "associate_public_ip_address": "false", "root_volume_gib": True,
                 "source_commit": "HEAD", "subnet_id": "subnet-x; command", "key_name": "https://example.com"}
        for field, value in cases.items():
            with self.subTest(field=field):
                candidate = config(); candidate[field] = value
                with self.assertRaises(aws.BuildError): aws.validate_config(candidate)

    def test_extra_missing_and_duplicate_keys_are_rejected_without_echo(self):
        candidate = config(); candidate["service_token"] = "SECRET_MUST_NOT_BE_ECHOED"
        with self.assertRaises(aws.BuildError) as result: aws.validate_config(candidate)
        self.assertNotIn("SECRET_MUST_NOT_BE_ECHOED", str(result.exception))
        candidate = config(); del candidate["associate_public_ip_address"]
        with self.assertRaises(aws.BuildError): aws.validate_config(candidate)
        with self.assertRaises(aws.BuildError): aws.parse_json(b'{"x":1,"x":2}')
        with self.assertRaises(aws.BuildError): aws.parse_json(b'{"x":NaN}')

    def test_disk_and_groups_are_bounded(self):
        for field, values in {"root_volume_gib": [9, 65, 10.0],
                              "security_group_ids": [[], ["sg-01234567"] * 2, [None], "sg-01234567"]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    candidate = config(); candidate[field] = value
                    with self.assertRaises(aws.BuildError): aws.validate_config(candidate)
        for size in (10, 64):
            candidate = config(); candidate["root_volume_gib"] = size
            self.assertEqual(aws.validate_config(candidate)["root_volume_gib"], size)

    def test_remote_configuration_accounts_and_extra_commands_are_rejected(self):
        variants = []
        candidate = ignition(); candidate["passwd"] = {"users": []}; variants.append(candidate)
        candidate = ignition(); candidate["ignition"]["config"] = {"merge": [{"source": "https://example.com"}]}; variants.append(candidate)
        candidate = ignition(); candidate["storage"]["files"][0]["contents"]["source"] = "https://example.com"; variants.append(candidate)
        candidate = ignition(); candidate["systemd"]["units"][0]["contents"] += "ExecStart=/bin/sh\n"; variants.append(candidate)
        for candidate in variants:
            with self.assertRaises(aws.BuildError): aws.validate_ignition(candidate)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "machine").mkdir()
        (self.root / "machine/sovereign-linux.bu").write_bytes((ROOT / "machine/sovereign-linux.bu").read_bytes())
        self.config_path = self.root / "input.json"
        self.config_path.write_text(json.dumps(config()))
        self.compiler = self.root / "butane"
        self.compiler_bytes = b"TEST FIXTURE ONLY - NOT A REAL COMPILER"
        self.compiler.write_bytes(self.compiler_bytes)
        self.repo_patch = patch.object(aws, "REPO", self.root)
        self.repo_patch.start(); self.addCleanup(self.repo_patch.stop)

    def fake_run(self, argv, **kwargs):
        self.assertEqual(Path(argv[0]).read_bytes(), self.compiler_bytes)
        self.assertNotEqual(Path(argv[0]), self.compiler)
        self.assertEqual(argv[1:], ["--strict"])
        self.assertEqual(aws.digest(kwargs["input"]), aws.SOURCE_SHA256)
        return subprocess.CompletedProcess(argv, 0, json.dumps(ignition()).encode(), b"")

    def generate(self, output=None):
        with patch.object(aws, "BUTANE_SHA256", aws.digest(self.compiler_bytes)), \
                patch.object(aws.subprocess, "run", side_effect=self.fake_run):
            return aws.build(self.config_path, self.compiler, output)

    def test_checksum_mismatch_stops_before_execution(self):
        with patch.object(aws.subprocess, "run") as run:
            with self.assertRaisesRegex(aws.BuildError, "compiler was not executed"):
                aws.build(self.config_path, self.compiler)
            run.assert_not_called()
        self.assertFalse((self.root / "build").exists())

    def test_source_change_stops_before_execution(self):
        (self.root / "machine/sovereign-linux.bu").write_text("changed")
        with patch.object(aws.subprocess, "run") as run:
            with self.assertRaisesRegex(aws.BuildError, "Base configuration"):
                self.generate()
            run.assert_not_called()

    def test_oversized_config_and_compiler_output_are_rejected(self):
        self.config_path.write_bytes(b" " * (aws.MAX_CONFIG_BYTES + 1))
        with patch.object(aws.subprocess, "run") as run:
            with self.assertRaisesRegex(aws.BuildError, "Configuration exceeds"):
                aws.build(self.config_path, self.compiler)
            run.assert_not_called()
        self.config_path.write_text(json.dumps(config()))
        oversized = subprocess.CompletedProcess([], 0, b" " * (aws.MAX_COMPILER_OUTPUT + 1), b"")
        with patch.object(aws, "BUTANE_SHA256", aws.digest(self.compiler_bytes)), \
                patch.object(aws.subprocess, "run", return_value=oversized):
            with self.assertRaisesRegex(aws.BuildError, "Compiler output exceeds"):
                aws.build(self.config_path, self.compiler)

    def test_fixed_launch_controls_and_hashes(self):
        output = self.generate()
        self.assertTrue(output.name.startswith("aws."))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        raw = (output / "machine.ign").read_bytes()
        template = json.loads((output / "launch-template-data.json").read_bytes())
        manifest = json.loads((output / "manifest.json").read_bytes())
        self.assertLessEqual(len(raw), 16384)
        self.assertEqual(base64.b64decode(template["UserData"]), raw)
        self.assertEqual(template["MetadataOptions"], {"HttpEndpoint": "enabled", "HttpTokens": "required",
                         "HttpPutResponseHopLimit": 1, "InstanceMetadataTags": "disabled"})
        self.assertEqual(template["CreditSpecification"], {"CpuCredits": "standard"})
        self.assertEqual(template["BlockDeviceMappings"][0]["Ebs"], {
            "VolumeType": "gp3", "VolumeSize": 16, "Encrypted": True, "DeleteOnTermination": True})
        self.assertFalse(template["NetworkInterfaces"][0]["AssociatePublicIpAddress"])
        self.assertNotIn("IamInstanceProfile", template)
        self.assertEqual(manifest["states"]["instances"], "not_launched")
        self.assertFalse(manifest["aws_checks"]["performed"])
        for name, expected in manifest["outputs"].items():
            artifact = output / name
            self.assertEqual(aws.digest(artifact.read_bytes()), expected["sha256"])
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_no_overwrite_no_outside_path_and_no_symlink(self):
        existing = self.generate(self.root / "build/aws.existing")
        original = (existing / "manifest.json").read_bytes()
        with self.assertRaises(aws.BuildError): self.generate(existing)
        self.assertEqual((existing / "manifest.json").read_bytes(), original)
        with self.assertRaises(aws.BuildError): self.generate(self.root / "aws.outside")
        (self.root / "build/aws.link").symlink_to(self.root / "absent")
        with self.assertRaises(aws.BuildError): self.generate(self.root / "build/aws.link")

    def test_ignition_limit_and_failed_compile_leave_no_package(self):
        with patch.object(aws, "MAX_IGNITION_BYTES", 32):
            with self.assertRaisesRegex(aws.BuildError, "user-data size limit"): self.generate()
        self.assertFalse((self.root / "build").exists())
        with patch.object(aws, "BUTANE_SHA256", aws.digest(self.compiler_bytes)), \
                patch.object(aws.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"SECRET")):
            with self.assertRaises(aws.BuildError) as result: aws.build(self.config_path, self.compiler)
            self.assertNotIn("SECRET", str(result.exception))
        self.assertFalse((self.root / "build").exists())

    def test_headless_profile_omits_key_name(self):
        settings = config(); settings["key_name"] = None
        self.config_path.write_text(json.dumps(settings))
        output = self.generate()
        template = json.loads((output / "launch-template-data.json").read_bytes())
        self.assertNotIn("KeyName", template)
        self.assertNotIn("IamInstanceProfile", template)

    @unittest.skipUnless((ROOT / "build/tools/butane").is_file(), "Pinned Butane is not locally available")
    def test_real_pinned_butane_compiles_only_the_existing_base(self):
        output = aws.build(self.config_path, ROOT / "build/tools/butane")
        compiled = json.loads((output / "machine.ign").read_bytes())
        self.assertEqual(compiled, ignition())
        self.assertEqual(json.loads((output / "manifest.json").read_bytes())["compiler"]["sha256"],
                         "53a20d820fbaa7fda4f1afd1814974badc8e448db4c151d3f1ba005dc29bc4c9")


if __name__ == "__main__":
    unittest.main()
