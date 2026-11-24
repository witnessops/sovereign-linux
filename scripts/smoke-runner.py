"""Run the bundled synthetic fixture once using a new rootless n8n main and guest."""
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess as sp
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request

REPO = Path(__file__).resolve().parents[1]
MAIN = "0fc7fd41adb46f4b70f1c8501325aab91f28363df6d2dd567720372def7037c2"
RUNNER = "8045f3fa0f22bb5e140133c95bebed0f7293867a0b3a47a1dc5453b0d61ea689"
FIXTURE = "500d50173083a1b3fb699f2c3099b1dd0bc3a866b481a40f0e68c60fc06fad91"
MARKER = "wops-vm-smoke-20261113"


def run(*args, timeout=90, **kwargs):
    return sp.run(args, text=True, stdout=sp.PIPE, check=True, timeout=timeout, **kwargs).stdout.strip()


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


def smoke(bundle, evidence):
    fixture = (REPO / "runner/smoke-workflow.json").read_bytes()
    if hashlib.sha256(fixture).hexdigest() != FIXTURE:
        raise ValueError("Synthetic fixture checksum mismatch")
    for name in ("runner.iso", "machine.ign", "machine.qcow2"):
        if not (bundle / name).is_file():
            raise ValueError("Missing bundle file: " + name)
    run("sha256sum", "-c", "SHA256SUMS", cwd=bundle)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    name = "sovereign-smoke-" + secrets.token_hex(6)
    db_dir = REPO / "build" / name
    db_dir.mkdir(mode=0o700)
    print("Private SQLite directory:", db_dir, flush=True)
    token = secrets.token_hex(32)
    cleanup = {}
    try:
        with ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="sovereign-smoke-")))
            # Keep the database after shutdown for independent, read-only verification.
            run("podman", "network", "create", "--internal", name)
            stack.callback(run, "podman", "network", "rm", name)
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
            cid = run("podman", "create", "--pull=never", "--name", name, "--network", name,
                      "--userns=keep-id:uid=1000,gid=1000", "-v", f"{db_dir}:/home/node/.n8n:rw",
                      "-p", "127.0.0.1::5678", "-p", "127.0.0.1::5679", *env_args, MAIN, env=env)
            stack.callback(run, "podman", "rm", "-v", cid)
            run("podman", "start", cid)

            def stop_main():
                run("podman", "stop", "-t", "20", cid)
                cleanup["main_stopped"] = run("podman", "inspect", "--format", "{{.State.Running}}", cid) == "false"

            stack.callback(stop_main)
            ui, broker = [int(run("podman", "port", cid, str(n)).split(":")[-1]) for n in (5678, 5679)]
            api = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def request(path, body=None):
                req = Request(f"http://127.0.0.1:{ui}/rest" + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Content-Type": "application/json"})
                with api.open(req, timeout=15) as response:
                    return json.load(response)["data"]

            def settings_ready():
                try:
                    return request("/settings")
                except (URLError, ConnectionError, json.JSONDecodeError):
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
            logs = run("podman", "logs", "--timestamps", cid, stderr=sp.STDOUT)
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
    parser.add_argument("bundle", type=Path)
    parser.add_argument("evidence", type=Path, help="new private evidence directory")
    args = parser.parse_args()
    os.umask(0o077)
    smoke(args.bundle.resolve(), args.evidence.resolve())
