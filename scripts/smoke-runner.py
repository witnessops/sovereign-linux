"""Run the bundled synthetic fixture once using a new rootless n8n main and guest."""
import argparse
import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import subprocess as sp
import tempfile
import time
from typing import NamedTuple
from urllib.error import HTTPError

REPO = Path(__file__).resolve().parents[1]
MAIN = "0fc7fd41adb46f4b70f1c8501325aab91f28363df6d2dd567720372def7037c2"
RUNNER = "8045f3fa0f22bb5e140133c95bebed0f7293867a0b3a47a1dc5453b0d61ea689"
FIXTURE = "500d50173083a1b3fb699f2c3099b1dd0bc3a866b481a40f0e68c60fc06fad91"
MARKER = "wops-vm-smoke-20261113"
REQUEST_LABEL = "io.sovereignlinux.smoke.request"
ATTEMPT_LABEL = "io.sovereignlinux.smoke.attempt"
MAIN_API_CLIENT = """
const http = require('node:http');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const frame = JSON.parse(input);
  const body = frame.body === null ? null : JSON.stringify(frame.body);
  const headers = { 'Content-Type': 'application/json' };
  if (frame.cookie) headers.Cookie = frame.cookie;
  if (body !== null) headers['Content-Length'] = Buffer.byteLength(body);
  const request = http.request({
    host: '127.0.0.1', port: 5678, path: '/rest' + frame.path,
    method: body === null ? 'GET' : 'POST', headers,
  }, response => {
    const chunks = [];
    response.on('data', chunk => { chunks.push(chunk); });
    response.on('end', () => {
      process.stdout.write(JSON.stringify({
        status: response.statusCode,
        cookies: response.headers['set-cookie'] || [],
        body: Buffer.concat(chunks).toString('utf8'),
      }));
    });
  });
  request.on('error', error => {
    process.stderr.write(error.message + '\\n');
    process.exitCode = 1;
  });
  request.end(body === null ? undefined : body);
});
"""


def run(*args, timeout=90, network_config_dir=None, **kwargs):
    if args[0] == "podman":
        options = ["--remote=false"]
        if network_config_dir is not None:
            options.append(f"--network-config-dir={network_config_dir}")
        args = ("podman", *options, *args[1:])
    return sp.run(args, text=True, stdout=sp.PIPE, check=True, timeout=timeout, **kwargs).stdout.strip()


class MainApi:
    def __init__(self, container, store):
        self.store = store
        self.container = container
        self.cookies = {}

    def request(self, path, body=None):
        frame = {"path": path, "body": body,
                 "cookie": "; ".join(f"{name}={value}" for name, value in self.cookies.items())}
        raw = self.store.podman("exec", "-i", self.container, "node", "-e", MAIN_API_CLIENT,
                                input=json.dumps(frame), timeout=15)
        response = json.loads(raw)
        if (not isinstance(response, dict) or type(response.get("status")) is not int
                or not isinstance(response.get("cookies"), list)
                or not isinstance(response.get("body"), str)):
            raise ValueError("Unexpected n8n API response")
        for cookie in response["cookies"]:
            if not isinstance(cookie, str) or "=" not in cookie:
                raise ValueError("Unexpected n8n API cookie")
            name, value = cookie.split(";", 1)[0].split("=", 1)
            self.cookies[name] = value
        if not 200 <= response["status"] < 300:
            raise HTTPError("http://127.0.0.1:5678/rest" + path, response["status"],
                            "n8n API request failed", None, None)
        return json.loads(response["body"])["data"]


def record(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def until(probe, timeout, description):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = probe()
        if value:
            return value
        time.sleep(1)
    raise RuntimeError("Timed out waiting for " + description)


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except sp.TimeoutExpired:
            proc.kill()
    return proc.wait()


def terminate_on_sigterm(signum, frame):
    # Allow the active ExitStack and stop.json writer to finish after one stop request.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(128 + signum)


@contextmanager
def defer_sigterm():
    # Keep a new resource and its cleanup callback in the same signal-safe step.
    pending = []
    previous = signal.getsignal(signal.SIGTERM)

    def remember(signum, frame):
        pending.append(signum)

    signal.signal(signal.SIGTERM, remember)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        if pending and previous != signal.SIG_IGN:
            handler = previous if callable(previous) else terminate_on_sigterm
            handler(pending[0], None)


def configure_guest(bundle, tmp, port):
    for key in ("client", "host"):
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", tmp / key)
    host_pub = (tmp / "host.pub").read_text().split()
    (tmp / "known_hosts").write_text(f"[127.0.0.1]:{port} {host_pub[0]} {host_pub[1]}\n")
    config = json.loads((bundle / "machine.ign").read_bytes())
    config["passwd"] = {"users": [{"name": "core", "sshAuthorizedKeys": [(tmp / "client.pub").read_text().strip()]}]}
    for path, content, mode in (
        ("/etc/ssh/ssh_host_ed25519_key", (tmp / "host").read_bytes(), 0o600),
        ("/etc/ssh/ssh_host_ed25519_key.pub", (tmp / "host.pub").read_bytes(), 0o644),
        ("/etc/ssh/sshd_config.d/40-runner.conf", b"PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers core\n", 0o644),
        ("/var/lib/systemd/linger/core", b"", 0o644),
    ):
        source = "data:;base64," + base64.b64encode(content).decode()
        config["storage"]["files"].append({"path": path, "mode": mode, "contents": {"source": source}})
    (tmp / "run.ign").write_text(json.dumps(config))


def saved_result(serialized):
    # In Flatted, strings inside objects and arrays are indices into the root table.
    table = json.loads(serialized)

    def field(obj, key):
        item = obj[key]
        return table[int(item)] if isinstance(item, str) else item

    result = field(table[0], "resultData")
    runs = field(field(result, "runData"), "VM Code smoke")
    if len(runs) != 1 or field(result, "lastNodeExecuted") != "VM Code smoke":
        raise ValueError("Unexpected Code node execution count or last node")
    items = field(field(field(field(runs, 0), "data"), "main"), 0)
    if len(items) != 1:
        raise ValueError("Expected one Code output item")
    item = field(field(items, 0), "json")
    number = field(item, "result")
    if type(number) is not int or number != 42 or field(item, "marker") != MARKER:
        raise ValueError("Saved Code output differs from the synthetic fixture")
    return {"code_runs": len(runs), "items": len(items), "marker": MARKER, "result": number}


class PodmanStore(NamedTuple):
    fingerprint: str
    network_dir: Path

    def check_current(self):
        if rootless_store() != self:
            raise RuntimeError("Podman store changed before cleanup")

    def podman(self, *args, **kwargs):
        return run("podman", *args, network_config_dir=self.network_dir, **kwargs)


def rootless_store(*, prepare=False):
    uid = os.geteuid()
    if uid == 0:
        raise RuntimeError("The host n8n main requires rootless Podman")
    info = json.loads(run("podman", "info", "--format", "json", timeout=10))
    host, store = info["host"], info["store"]
    if host["security"]["rootless"] is not True or host["serviceIsRemote"] is not False:
        raise RuntimeError("The host n8n main requires local rootless Podman")
    if host.get("networkBackend") != "netavark":
        raise RuntimeError("The smoke profile requires the Netavark network backend")
    root = Path(store["graphRoot"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Expected a local Podman graph root directory")
    root = root.resolve(strict=True)
    meta = root.stat()
    machine = Path("/etc/machine-id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", machine):
        raise ValueError("Unavailable local machine identity")
    network_dir = root / "networks"
    if prepare:
        network_dir.mkdir(mode=0o700, exist_ok=True)
    network_dir = network_dir.resolve(strict=True)
    if not network_dir.is_dir():
        raise ValueError("Expected a Netavark configuration directory")
    network_meta = network_dir.stat()
    identity = [machine, uid, str(root), meta.st_dev, meta.st_ino, store["runRoot"],
                store["graphDriverName"], host["networkBackend"],
                str(network_dir), network_meta.st_dev, network_meta.st_ino]
    return PodmanStore(hashlib.sha256(json.dumps(identity).encode()).hexdigest(), network_dir)


def request_id_for(evidence):
    if re.fullmatch(r"smoke_[0-9a-f]{32}", evidence.name):
        return evidence.name
    # Standalone callers can choose arbitrary directory names. Keep them off Podman labels.
    return "local_" + hashlib.sha256(os.fsencode(evidence.name)).hexdigest()


def save_resource_intent(evidence, name, store):
    intent = {"request_id": request_id_for(evidence), "name": name, "attempt": secrets.token_hex(16),
              "store_id": store.fingerprint, "network_config_dir": str(store.network_dir)}
    record(evidence / "resource-intent.json", intent)
    for directory in (evidence, evidence.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return intent


def inspect_resource(kind, intent, store):
    name = intent["name"]
    try:
        store.podman(kind, "exists", name, timeout=10, stderr=sp.DEVNULL)
    except sp.CalledProcessError as error:
        if error.returncode == 1:
            return {"state": "absent"}
        raise

    label_field = ".Config.Labels" if kind == "container" else ".Labels"
    fields = "{{.Name}} {{.ID}} {{json " + label_field + "}}"
    raw = store.podman(kind, "inspect", "--format", fields, name, timeout=10, stderr=sp.DEVNULL)
    actual_name, resource_id, raw_labels = raw.split(" ", 2)
    labels = json.loads(raw_labels)
    if actual_name != name or not re.fullmatch(r"[0-9a-f]{64}", resource_id):
        return {"state": "unknown", "reason": "unexpected_identity"}
    if labels is not None and not isinstance(labels, dict):
        return {"state": "unknown", "reason": "invalid_labels"}
    if (not labels or labels.get(REQUEST_LABEL) != intent["request_id"]
            or labels.get(ATTEMPT_LABEL) != intent["attempt"]):
        return {"state": "foreign"}
    return {"state": "owned", "id": resource_id}


def remove_network(network_id, store):
    store.check_current()
    try:
        store.podman("network", "rm", network_id)
    except sp.CalledProcessError as error:
        # For one full network ID, exit 1 means the original is already gone.
        if error.returncode != 1:
            raise


def inspect_resources(evidence):
    request_id = request_id_for(evidence)
    try:
        intent = json.loads((evidence / "resource-intent.json").read_bytes())
    except FileNotFoundError:
        return {"request_id": request_id, "state": "unknown", "reason": "missing_intent"}
    except (OSError, ValueError):
        intent = None
    if (not isinstance(intent, dict) or intent.get("request_id") != request_id
            or not isinstance(intent.get("name"), str) or not re.fullmatch(r"sovereign-smoke-[0-9a-f]{12}", intent["name"])
            or not isinstance(intent.get("attempt"), str) or not re.fullmatch(r"[0-9a-f]{32}", intent["attempt"])):
        return {"request_id": request_id, "state": "unknown", "reason": "invalid_intent"}

    if not isinstance(intent.get("store_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", intent["store_id"]):
        return {"request_id": request_id, "state": "unknown", "reason": "unverified_store"}
    if not isinstance(intent.get("network_config_dir"), str) or not Path(intent["network_config_dir"]).is_absolute():
        return {"request_id": request_id, "state": "unknown", "reason": "unverified_network_config"}
    try:
        store = rootless_store()
        if store.fingerprint == intent["store_id"] and str(store.network_dir) == intent["network_config_dir"]:
            resources = {}
            for kind in ("container", "network"):
                try:
                    resources[kind] = inspect_resource(kind, intent, store)
                except (OSError, ValueError, sp.SubprocessError):
                    resources[kind] = {"state": "unknown", "reason": "inspection_failed"}
            if rootless_store() == store:
                return {"request_id": request_id, "resources": resources}
        return {"request_id": request_id, "state": "unknown", "reason": "store_mismatch"}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sp.SubprocessError):
        return {"request_id": request_id, "state": "unknown", "reason": "rootless_host_unavailable"}


def smoke(bundle, evidence):
    fixture = (REPO / "runner/smoke-workflow.json").read_bytes()
    if hashlib.sha256(fixture).hexdigest() != FIXTURE:
        raise ValueError("Synthetic fixture checksum mismatch")
    for name in ("runner.iso", "machine.ign", "machine.qcow2"):
        if not (bundle / name).is_file():
            raise ValueError("Missing bundle file: " + name)
    run("sha256sum", "-c", "SHA256SUMS", cwd=bundle)
    store = rootless_store(prepare=True)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    name = "sovereign-smoke-" + secrets.token_hex(6)
    intent = save_resource_intent(evidence, name, store)
    labels = ["--label", f"{REQUEST_LABEL}={intent['request_id']}",
              "--label", f"{ATTEMPT_LABEL}={intent['attempt']}"]
    db_dir = REPO / "build" / name
    db_dir.mkdir(mode=0o700)
    print("Private SQLite directory:", db_dir, flush=True)
    token = secrets.token_hex(32)
    cleanup = {}
    try:
        with ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="sovereign-smoke-")))
            # Keep the database after shutdown for independent, read-only verification.
            with defer_sigterm():
                store.podman("network", "create", "--internal", *labels, name)
                network = inspect_resource("network", intent, store)
                if network["state"] != "owned":
                    raise RuntimeError("Could not verify the new network's ownership")
                network_id = network["id"]
                if network_id == hashlib.sha256(name.encode()).hexdigest():
                    raise RuntimeError("Podman returned a name-derived network ID")
                network_removal_safe = True

                def cleanup_network():
                    if network_removal_safe:
                        remove_network(network_id, store)
                    else:
                        cleanup["network_cleanup"] = "deferred"

                stack.callback(cleanup_network)
            env = dict(os.environ, N8N_RUNNERS_AUTH_TOKEN=token, N8N_ENCRYPTION_KEY=secrets.token_hex(32))
            settings = ["N8N_RUNNERS_MODE=external", "N8N_RUNNERS_ENABLED=true",
                        "N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0", "N8N_RUNNERS_MAX_CONCURRENCY=1",
                        "N8N_RUNNERS_TASK_REQUEST_TIMEOUT=240", "N8N_RUNNERS_TASK_TIMEOUT=60",
                        "N8N_RUNNERS_MAX_PAYLOAD=1048576", "N8N_RUNNERS_INSECURE_MODE=false",
                        "EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS=true", "N8N_SECURE_COOKIE=false",
                        "N8N_DIAGNOSTICS_ENABLED=false", "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
                        "N8N_TEMPLATES_ENABLED=false", "N8N_COMMUNITY_PACKAGES_ENABLED=false",
                        "N8N_PERSONALIZATION_ENABLED=false", "N8N_RUNNERS_AUTH_TOKEN", "N8N_ENCRYPTION_KEY"]
            env_args = [arg for setting in settings for arg in ("-e", setting)]
            with defer_sigterm():
                # Podman may persist the attachment before returning a container ID.
                network_removal_safe = False
                cid = store.podman("create", "--pull=never", *labels, "--name", name, "--network", network_id,
                                   "--userns=keep-id:uid=1000,gid=1000", "-v", f"{db_dir}:/home/node/.n8n:rw",
                                   "-p", "127.0.0.1::5679", *env_args, MAIN, env=env)
                if not re.fullmatch(r"[0-9a-f]{64}", cid):
                    raise RuntimeError("Container create returned no valid full ID")

                def remove_main():
                    nonlocal network_removal_safe
                    store.check_current()
                    store.podman("rm", "-v", cid)
                    network_removal_safe = True

                stack.callback(remove_main)

                def stop_main():
                    store.check_current()
                    if store.podman("inspect", "--format", "{{.State.Running}}", cid) == "true":
                        store.podman("stop", "-t", "20", cid)
                    cleanup["main_stopped"] = store.podman("inspect", "--format", "{{.State.Running}}", cid) == "false"

                stack.callback(stop_main)
                store.podman("start", cid)
            broker = int(store.podman("port", cid, "5679").split(":")[-1])
            request = MainApi(cid, store).request

            def settings_ready():
                try:
                    return request("/settings")
                except (sp.CalledProcessError, sp.TimeoutExpired, HTTPError, json.JSONDecodeError):
                    return None

            settings_data = until(settings_ready, 120, "fresh n8n main")
            if not settings_data["userManagement"]["showSetupOnFirstLoad"]:
                raise RuntimeError("Expected an unowned disposable main")
            owner = {"email": "smoke@example.invalid", "firstName": "Local", "lastName": "Smoke",
                     "password": "Aa9!" + secrets.token_hex(20)}
            request("/owner/setup", owner)
            workflow = request("/workflows/", json.loads(fixture))
            wid = workflow["id"]
            if workflow["active"] is not False:
                raise RuntimeError("Expected an inactive fixture")
            print("Fresh main ready. Starting isolated Sovereign Linux guest.", flush=True)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                ssh_port = probe.getsockname()[1]
            configure_guest(bundle, tmp, ssh_port)
            kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
            accel, cpu = ("kvm", "host") if kvm else ("tcg", "max")
            diag = stack.enter_context((evidence / "qemu.log").open("xb"))
            with defer_sigterm():
                qemu = sp.Popen(["qemu-system-x86_64", "-name", name, "-machine", "q35,dump-guest-core=off",
                                 "-accel", accel, "-cpu", cpu, "-smp", "2", "-m", "3072",
                                 "-drive", "if=virtio,format=qcow2,file=machine.qcow2,snapshot=on",
                                 "-drive", "if=virtio,format=raw,readonly=on,file=runner.iso",
                                 "-fw_cfg", f"name=opt/com.coreos/config,file={tmp}/run.ign",
                                 "-netdev", f"user,id=lab,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
                                 "-device", "virtio-net-pci,netdev=lab", "-device", "virtio-rng-pci",
                                 "-sandbox", "on,obsolete=deny,elevateprivileges=deny,resourcecontrol=deny",
                                 "-display", "none", "-monitor", "none", "-no-reboot",
                                 "-serial", f"file:{evidence}/serial.log"], cwd=bundle, stdin=sp.DEVNULL,
                                stdout=diag, stderr=diag, env=dict(os.environ, TMPDIR=str(tmp)))

                def stop_guest():
                    cleanup["qemu_exit"] = stop(qemu)
                    cleanup["vm_stopped"] = qemu.poll() is not None

                stack.callback(stop_guest)
            ssh = ["ssh", "-F", "/dev/null", "-p", str(ssh_port), "-i", str(tmp/"client"),
                   "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
                   "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={tmp}/known_hosts",
                   "-o", "ConnectTimeout=3"]
            target = "core@127.0.0.1"

            def guest_ready():
                if qemu.poll() is not None:
                    raise RuntimeError("VM exited; inspect qemu.log")
                try:
                    return run(*ssh, target, "test -r /var/mnt/runner/runner.tar && hostname", timeout=8,
                               stderr=sp.DEVNULL) == "sovereign-linux"
                except (sp.CalledProcessError, sp.TimeoutExpired):
                    return False

            until(guest_ready, 600, "pinned SSH and read-only runner media")
            with defer_sigterm():
                tunnel = sp.Popen([*ssh, "-o", "ExitOnForwardFailure=yes", "-N", "-R",
                                   f"127.0.0.1:5679:127.0.0.1:{broker}", target], stdin=sp.DEVNULL)
                stack.callback(stop, tunnel)

            def broker_ready():
                if tunnel.poll() is not None:
                    raise RuntimeError("Broker tunnel exited")
                try:
                    return run(*ssh, target, "curl -fsS --max-time 3 -o /dev/null -w %{http_code} http://127.0.0.1:5679/healthz",
                               timeout=10, stderr=sp.DEVNULL) == "200"
                except (sp.CalledProcessError, sp.TimeoutExpired):
                    return False

            until(broker_ready, 45, "guest broker")
            print("Guest broker ready. Loading the pinned rootless runner.", flush=True)
            run(*ssh, target, "bash /var/mnt/runner/start.sh", input=token+"\n", timeout=750)
            inspect = run(*ssh, target, "podman inspect --format '{{.Id}} {{.Image}} {{.Config.User}} {{.State.Running}} {{.HostConfig.ReadonlyRootfs}}' sovereign-n8n-runner")
            runner_id, image, user, running, readonly = inspect.split()
            if (image, user, running, readonly) != (RUNNER, "65532:65532", "true", "true"):
                raise RuntimeError("Unexpected guest runner identity or restrictions")

            def runner_ready():
                logs = run(*ssh, target, "podman logs sovereign-n8n-runner 2>&1")
                return "Waiting for launcher's task offer to be accepted" in logs

            until(runner_ready, 150, "external runner offer")
            record(evidence / "dispatch-intent.json", {"time": datetime.now(timezone.utc).isoformat(),
                   "bundle": bundle.name, "acceleration": accel, "main_image": MAIN, "main_container": cid,
                   "workflow_id": wid, "fixture_sha256": FIXTURE, "runner_image": image,
                   "runner_container": runner_id, "runner_user": user, "runner_root_read_only": True,
                   "guest_hostname": "sovereign-linux", "database": str(db_dir/"database.sqlite")})
            print("Submitting the manual trigger once.", flush=True)
            eid = request(f"/workflows/{wid}/run", {"triggerToStartFrom": {"name": "Manual Trigger"}})["executionId"]
            record(evidence / "execution.json", {"workflow_id": wid, "execution_id": eid})

            def finished():
                try:
                    execution = request(f"/executions/{eid}")
                except HTTPError as error:
                    if error.code == 404:
                        return None
                    raise
                return execution if execution.get("stoppedAt") or execution["status"] in ("success","error","crashed","canceled","failed") else None

            execution = until(finished, 330, "submitted execution " + str(eid))
            if str(execution["id"]) != str(eid) or execution["workflowId"] != wid or execution["status"] != "success" or execution["finished"] is not True:
                raise RuntimeError("Execution failed its saved API readback: " + execution["status"])
            api_result = saved_result(execution["data"])
            logs = store.podman("logs", "--timestamps", cid, stderr=sp.STDOUT)
            registrations = [line.strip() for line in logs.splitlines()
                             if 'Registered runner "JS Task Runner"' in line
                             or 'Registered runner "launcher-javascript"' in line]
            record(evidence / "broker.json", {"main_container": cid, "guest_runner": runner_id,
                                              "registrations": registrations})
            print(f"Saved API execution {eid} returned 42. Stopping guest and main.", flush=True)
    finally:
        record(evidence / "stop.json", cleanup)

    with sqlite3.connect((db_dir/"database.sqlite").as_uri()+"?mode=ro", uri=True) as db:
        count, = db.execute("SELECT count(*) FROM execution_entity").fetchone()
        row = db.execute('SELECT e.status,e.finished,e."workflowId",d.data FROM execution_entity e JOIN execution_data d ON e.id=d."executionId" WHERE e.id=?', (eid,)).fetchone()
    if count != 1 or row is None or row[:3] != ("success", 1, wid):
        raise RuntimeError("Independent SQLite identity or execution count mismatch")
    checked = saved_result(row[3])
    if checked != api_result or not cleanup["vm_stopped"] or not cleanup["main_stopped"]:
        raise RuntimeError("Readback or shutdown mismatch")
    record(evidence / "result.json", {"workflow_id": wid, "execution_id": eid, "status": "success",
           "database_executions": count, "readbacks": ["n8n execution API", "stopped main SQLite"],
           **checked, **cleanup})
    print("Verified the stopped database and VM shutdown.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("bundle", type=Path, nargs="?")
    mode.add_argument("--inspect-resources", action="store_true",
                      help="read the ownership record and current rootless Podman resources")
    parser.add_argument("evidence", type=Path, help="new private evidence directory")
    args = parser.parse_args()
    os.umask(0o077)
    if args.inspect_resources:
        snapshot = inspect_resources(args.evidence.absolute())
        print(json.dumps(snapshot, sort_keys=True))
        resources = snapshot.get("resources", {})
        raise SystemExit(0 if resources and all(item["state"] in ("owned", "absent")
                                                for item in resources.values()) else 1)
    signal.signal(signal.SIGTERM, terminate_on_sigterm)
    smoke(args.bundle.resolve(), args.evidence.resolve())
