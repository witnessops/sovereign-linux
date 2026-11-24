#!/usr/bin/env python3
"""Prepare a bounded FCOS AWS launch package offline; never call AWS or launch."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote_to_bytes

REPO = Path(__file__).resolve().parents[1]
BUTANE_SHA256 = "53a20d820fbaa7fda4f1afd1814974badc8e448db4c151d3f1ba005dc29bc4c9"
SOURCE_SHA256 = "103294c5a09e19ec4c5b3515893f8d91d5257297305c1a639b1d45c6fd90e23f"
PIN = {
    "release": "44.20261023.3.1", "architecture": "x86_64",
    "region": "eu-west-1", "ami_id": "ami-0d094a1da6f4cdb56",
    "expected_owner_id": "125523088429", "root_device_name": "/dev/xvda",
}
FIELDS = set(PIN) | {
    "instance_type", "subnet_id", "security_group_ids", "key_name",
    "associate_public_ip_address", "root_volume_gib", "source_commit",
}
MAX_CONFIG_BYTES = 8192
MAX_IGNITION_BYTES = 16384
MAX_COMPILER_OUTPUT = 65536
BASE_UNIT = """[Unit]
Description=Check the Sovereign Linux base machine

[Service]
Type=oneshot
ExecStart=/usr/bin/grep -Fxq sovereign-linux /etc/hostname
ExecStart=/usr/bin/podman --version
ExecStart=/usr/bin/echo SOVEREIGN_LINUX_BASE_READY
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
"""


class BuildError(ValueError):
    """A safe diagnostic that does not contain caller-supplied values."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounded_read(path, limit, label):
    with Path(path).open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise BuildError(label + " exceeds its size limit")
    return data


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildError("JSON contains a duplicate key")
        result[key] = value
    return result


def parse_json(data):
    def invalid_constant(_):
        raise BuildError("Non-finite JSON numbers are not allowed")
    try:
        return json.loads(data, object_pairs_hook=object_pairs,
                          parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
        raise BuildError("Invalid JSON input") from error


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_config(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise BuildError("Configuration requires exactly the documented fields")
    for field, expected in PIN.items():
        if config[field] != expected:
            raise BuildError(field + " does not match the approved base tuple")
    if config["instance_type"] not in ("t3.small", "t3.medium"):
        raise BuildError("Unsupported instance_type")
    if not matches(config["subnet_id"], r"subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})"):
        raise BuildError("Invalid subnet_id")
    groups = config["security_group_ids"]
    if (not isinstance(groups, list) or not 1 <= len(groups) <= 5
            or any(not matches(g, r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})") for g in groups)
            or len(set(groups)) != len(groups)):
        raise BuildError("One to five distinct security_group_ids are required")
    if config["key_name"] is not None and not matches(config["key_name"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"):
        raise BuildError("key_name must be an existing name or null for a headless base check")
    if type(config["associate_public_ip_address"]) is not bool:
        raise BuildError("associate_public_ip_address must be an explicit boolean")
    if type(config["root_volume_gib"]) is not int or not 10 <= config["root_volume_gib"] <= 64:
        raise BuildError("root_volume_gib must be an integer from 10 through 64")
    if not matches(config["source_commit"], r"[0-9a-f]{40}"):
        raise BuildError("Invalid source_commit")
    return config


def validate_ignition(value):
    """Accept only the known base intent, not a general Ignition configuration."""
    if not isinstance(value, dict) or set(value) != {"ignition", "storage", "systemd"}:
        raise BuildError("Compiled Ignition has unexpected top-level content")
    if value["ignition"] != {"version": "3.5.0"}:
        raise BuildError("Unexpected compiled Ignition version or remote configuration")
    storage = value["storage"]
    systemd = value["systemd"]
    if (not isinstance(storage, dict) or set(storage) != {"files"}
            or not isinstance(storage["files"], list) or len(storage["files"]) != 1):
        raise BuildError("Compiled storage differs from the base")
    file = storage["files"][0]
    if (not isinstance(file, dict)
            or set(file) != {"path", "mode", "overwrite", "contents"}
            or file["path"] != "/etc/hostname" or type(file["mode"]) is not int
            or file["mode"] != 0o644 or file["overwrite"] is not True):
        raise BuildError("Compiled hostname file differs from the base")
    contents = file["contents"]
    if (not isinstance(contents, dict) or "source" not in contents
            or set(contents) - {"source", "compression"}
            or contents.get("compression", "") != ""):
        raise BuildError("Unexpected compiled file contents")
    source = contents["source"]
    if not isinstance(source, str):
        raise BuildError("Unexpected compiled file source")
    try:
        if source.startswith("data:,"):
            hostname = unquote_to_bytes(source[6:])
        elif source.startswith("data:;base64,"):
            hostname = base64.b64decode(source[13:], validate=True)
        else:
            raise BuildError("Only the inline hostname is allowed")
    except (ValueError, UnicodeError) as error:
        raise BuildError("Invalid inline hostname") from error
    if hostname != b"sovereign-linux\n":
        raise BuildError("Compiled hostname differs from the base")
    expected = {"units": [{"name": "sovereign-base-check.service",
                           "enabled": True, "contents": BASE_UNIT}]}
    if systemd != expected:
        raise BuildError("Compiled service differs from the base")
    return value


def compile_base(compiler, source):
    binary = bounded_read(compiler, 64 * 1024 * 1024, "Compiler")
    if digest(binary) != BUTANE_SHA256:
        raise BuildError("Butane SHA-256 mismatch; compiler was not executed")
    # Run a private verified copy, not a path that could have changed after hashing.
    with tempfile.TemporaryDirectory(prefix="sovereign-butane.") as temporary:
        executable = Path(temporary) / "butane"
        executable.write_bytes(binary)
        executable.chmod(0o700)
        result = subprocess.run([str(executable), "--strict"], input=source,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30, check=False)
    if result.returncode:
        raise BuildError("Verified Butane compilation failed")
    if len(result.stdout) > MAX_COMPILER_OUTPUT:
        raise BuildError("Compiler output exceeds its size limit")
    return validate_ignition(parse_json(result.stdout))


def encode_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def launch_template(config, ignition):
    template = {
        "ImageId": config["ami_id"], "InstanceType": config["instance_type"],
        "UserData": base64.b64encode(ignition).decode("ascii"),
        "CreditSpecification": {"CpuCredits": "standard"},
        "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required",
                            "HttpPutResponseHopLimit": 1, "InstanceMetadataTags": "disabled"},
        "BlockDeviceMappings": [{"DeviceName": config["root_device_name"],
            "Ebs": {"VolumeType": "gp3", "VolumeSize": config["root_volume_gib"],
                    "Encrypted": True, "DeleteOnTermination": True}}],
        "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": config["subnet_id"],
            "Groups": config["security_group_ids"], "DeleteOnTermination": True,
            "AssociatePublicIpAddress": config["associate_public_ip_address"]}],
    }
    if config["key_name"] is not None:
        template["KeyName"] = config["key_name"]
    return template


def build(config_path, butane_path, output_dir=None):
    config_bytes = bounded_read(config_path, MAX_CONFIG_BYTES, "Configuration")
    config = validate_config(parse_json(config_bytes))
    build_root = REPO / "build"
    if build_root.is_symlink():
        raise BuildError("The build directory must not be a symbolic link")
    destination = None
    if output_dir is not None:
        destination = Path(output_dir).absolute()
        if (destination.parent != build_root or not matches(destination.name, r"aws\.[A-Za-z0-9][A-Za-z0-9._-]*")
                or destination.exists() or destination.is_symlink()):
            raise BuildError("Output must be a new build/aws.* directory")
    source = bounded_read(REPO / "machine/sovereign-linux.bu", 16384, "Base configuration")
    if digest(source) != SOURCE_SHA256:
        raise BuildError("Base configuration SHA-256 mismatch")
    ignition = encode_json(compile_base(butane_path, source))
    if len(ignition) > MAX_IGNITION_BYTES:
        raise BuildError("Compact Ignition exceeds the EC2 user-data size limit")
    template = encode_json(launch_template(config, ignition))
    outputs = {"machine.ign": ignition, "launch-template-data.json": template}
    manifest = {
        "schema_version": 1, "profile": "sovereign-linux-aws-base-v0.1",
        "states": {"package": "prepared", "launch_template": "not_created",
                   "instances": "not_launched", "execution": "disabled"},
        "inputs": config, "configuration_sha256": digest(config_bytes),
        "source": {"path": "machine/sovereign-linux.bu", "sha256": digest(source),
                   "declared_commit": config["source_commit"], "git_commit_verified": False},
        "compiler": {"name": "Butane", "version": "0.29.0", "sha256": BUTANE_SHA256},
        "packager_sha256": digest(Path(__file__).read_bytes()),
        "aws_checks": {"performed": False,
                       "required_before_launch": ["caller_account", "ami_owner_and_state",
                           "network_rules_and_routes", "key_name", "cost_and_authority"]},
        "outputs": {name: {"sha256": digest(data), "bytes": len(data)}
                    for name, data in outputs.items()},
    }
    outputs["manifest.json"] = encode_json(manifest)
    build_root.mkdir(mode=0o700, exist_ok=True)
    if destination is None:
        destination = Path(tempfile.mkdtemp(prefix="aws.", dir=build_root))
    else:
        destination.mkdir(mode=0o700)
    try:
        for name, data in outputs.items():
            descriptor = os.open(destination / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--butane", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path,
                        help="New absolute REPO/build/aws.* path; otherwise create a unique directory")
    args = parser.parse_args()
    try:
        destination = build(args.config, args.butane, args.output_dir)
    except BuildError as error:
        print("BLOCKED: " + str(error), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print("BLOCKED: local I/O or compiler execution failed", file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared", "aws_status": "not_launched",
                      "output_directory": str(destination)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
