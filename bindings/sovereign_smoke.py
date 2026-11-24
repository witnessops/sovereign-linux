"""Local worker adapter for authenticated Sovereign Tasks commands. No listener."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILE = "sovereign-linux-n8n-smoke"
DEVICE = "ops-laptop-03"
REVISION = "eb382ec07dfe902d44b09e3ca8288329f0cc64fc"
BUNDLE = REPO / "build/machine.PToCA9g4"
MANIFEST = "1c7197b0492db12f0f7c668c2ab05595ed0abac2a8f6722b487c268142dbf428"
FIXTURE = "500d50173083a1b3fb699f2c3099b1dd0bc3a866b481a40f0e68c60fc06fad91"
MAIN = "0fc7fd41adb46f4b70f1c8501325aab91f28363df6d2dd567720372def7037c2"
RUNNER = "8045f3fa0f22bb5e140133c95bebed0f7293867a0b3a47a1dc5453b0d61ea689"
MARKER = "wops-vm-smoke-20261113"
STATE = Path.home() / ".local/state/sovereign-tasks" / PROFILE
TOOLS = {"run_sovereign_linux_smoke": "run", "read_sovereign_linux_smoke": "status"}


def broker_command(tool, arguments, *, device_id, claim_id):
    """Map an admitted owner request and its broker-held claim to the private worker frame."""
    if tool not in TOOLS or not isinstance(arguments, dict) or set(arguments) != {"request_id"}:
        raise ValueError("Unknown smoke tool or arguments")
    command = {"profile": PROFILE, "device_id": device_id, "operation": TOOLS[tool],
               "request_id": arguments["request_id"], "claim_id": claim_id}
    validate(command)
    return command


def check_target():
    # Bind the two files executed/read from the checkout even on a descendant branch.
    for name in ("scripts/smoke-runner.py", "runner/smoke-workflow.json"):
        pinned = subprocess.check_output(["git", "show", f"{REVISION}:{name}"], cwd=REPO)
        if (REPO / name).read_bytes() != pinned:
            raise ValueError(f"Changed pinned source: {name}")
    if hashlib.sha256((BUNDLE / "SHA256SUMS").read_bytes()).hexdigest() != MANIFEST:
        raise ValueError("Changed bundle manifest")
    # The pinned harness verifies every bundle file before creating a main or VM.


def launch(evidence):
    # Independent lifetime: loss of the broker response must not interrupt cleanup.
    # Keep slow boot and verification outside the broker round-trip timeout.
    with (evidence.parent / (evidence.name + ".log")).open("xb") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        subprocess.Popen([sys.executable, "-I", str(REPO / "scripts/smoke-runner.py"),
                          str(BUNDLE), str(evidence)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def valid_id(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def unique_fields(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate command field")
    return obj


def validate(command):
    required = {"profile", "device_id", "operation", "request_id", "claim_id"}
    if not isinstance(command, dict) or set(command) not in (required, required | {"execution_id"}):
        raise ValueError("Invalid command fields")
    if command["profile"] != PROFILE or command["device_id"] != DEVICE:
        raise ValueError("Wrong profile or device")
    if command["operation"] not in ("run", "status"):
        raise ValueError("Unknown operation")
    if not valid_id(command["request_id"], r"smoke_[0-9a-f]{32}") or not valid_id(command["claim_id"], r"claim_[0-9a-f]{32}"):
        raise ValueError("Invalid request or claim")
    if "execution_id" in command and (command["operation"] != "status" or
                                       not valid_id(command["execution_id"], r"[1-9][0-9]{0,19}")):
        raise ValueError("Invalid execution reference")


def evidence_record(directory, name):
    try:
        obj = json.loads((directory / name).read_bytes())
    except (FileNotFoundError, ValueError):
        # Harness records are exclusive-created, then written. A poll can see a partial file.
        return {}
    return obj if isinstance(obj, dict) else {}


def observe(directory):
    # The harness inherits the locked open file description on stdout/stderr.
    # A crash releases it without depending on a recycled PID or an exit hook.
    running = False
    try:
        with directory.with_suffix(".log").open("rb") as log:
            fcntl.flock(log, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        running = True
    except FileNotFoundError:
        pass
    intent = evidence_record(directory, "dispatch-intent.json")
    submitted = evidence_record(directory, "execution.json")
    workflow = intent.get("workflow_id")
    if (intent.get("bundle"), intent.get("fixture_sha256"), intent.get("main_image"),
        intent.get("runner_image")) != (BUNDLE.name, FIXTURE, MAIN, RUNNER):
        return {"state": "running" if running else "uncertain"}
    eid = submitted.get("execution_id")
    if not workflow or workflow != submitted.get("workflow_id") or not valid_id(eid, r"[1-9][0-9]{0,19}"):
        return {"state": "running" if running else "uncertain"}
    result = evidence_record(directory, "result.json")
    stop = evidence_record(directory, "stop.json")
    expected = {"workflow_id": workflow, "execution_id": eid, "status": "success",
                "database_executions": 1, "code_runs": 1, "items": 1, "marker": MARKER,
                "result": 42, "vm_stopped": True, "main_stopped": True, "qemu_exit": 0,
                "readbacks": ["n8n execution API", "stopped main SQLite"]}
    identity = {"execution_id": eid, "workflow_id": workflow}
    if not result and running:
        return {"state": "submitted", **identity}
    if (all(type(result.get(k)) is type(v) and result[k] == v for k, v in expected.items())
            and stop.get("vm_stopped") is True and stop.get("main_stopped") is True
            and type(stop.get("qemu_exit")) is int and stop["qemu_exit"] == 0):
        return {"state": "complete", **identity, "outcome": "success", "result": 42,
                "vm_stopped": True, "main_stopped": True}
    return {"state": "uncertain", **identity}


def handle(command, *, state=STATE, preflight=check_target, start=launch):
    validate(command)
    request, claim = command["request_id"], command["claim_id"]
    base = {"profile": PROFILE, "device_id": DEVICE, "request_id": request, "claim_id": claim,
            "build_revision": REVISION, "bundle_manifest_sha256": MANIFEST}
    database = state / "requests.sqlite"
    reading = command["operation"] == "status"
    if reading and not database.exists():
        return {**base, "state": "not_found"}
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = state.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise PermissionError("State directory must be private and owned by the worker")
    evidence = state / request
    with closing(sqlite3.connect(database, timeout=15)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS requests (
            request TEXT PRIMARY KEY, claim TEXT NOT NULL UNIQUE, revision TEXT NOT NULL,
            manifest TEXT NOT NULL, receipt TEXT)""")
        # The reservation must commit before the only possible call to start().
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT claim, revision, manifest FROM requests WHERE request=?", (request,)).fetchone()
            if row is None:
                if reading:
                    return {**base, "state": "not_found"}
                previous = db.execute("SELECT request, revision, manifest FROM requests WHERE receipt IS NULL LIMIT 1").fetchone()
                if previous:
                    if previous[1:] != (REVISION, MANIFEST):
                        raise ValueError("Unsettled request belongs to an earlier build")
                    saved = observe(state / previous[0])
                    if saved["state"] != "complete":
                        raise ValueError("Previous smoke request needs reconciliation")
                    db.execute("UPDATE requests SET receipt=? WHERE request=?", (json.dumps(saved), previous[0]))
                if evidence.exists():
                    raise ValueError("Orphan request evidence needs reconciliation")
                preflight()
                db.execute("INSERT INTO requests(request,claim,revision,manifest) VALUES (?,?,?,?)",
                           (request, claim, REVISION, MANIFEST))
            elif row[0] != claim:
                raise ValueError("Request belongs to a different claim")
        if row is None:
            try:
                start(evidence)
            except (OSError, subprocess.SubprocessError):
                # The child may have started. Reservation survives all transport failures.
                pass
        with db:
            db.execute("BEGIN IMMEDIATE")
            revision, manifest, receipt = db.execute(
                "SELECT revision, manifest, receipt FROM requests WHERE request=?", (request,)).fetchone()
            base.update(build_revision=revision, bundle_manifest_sha256=manifest)
            if not receipt and (revision, manifest) != (REVISION, MANIFEST):
                return {**base, "state": "uncertain"}
            answer = json.loads(receipt) if receipt else observe(evidence)
            if "execution_id" in command and answer.get("execution_id") != command["execution_id"]:
                raise ValueError("Execution does not match this request")
            if not receipt and answer["state"] == "complete":
                db.execute("UPDATE requests SET receipt=? WHERE request=?", (json.dumps(answer), request))
        return {**base, **answer}


if __name__ == "__main__":
    os.umask(0o077)
    # Local helper input only. The installed broker is responsible for authenticating the caller.
    frame = sys.stdin.buffer.read(4097)
    if len(frame) > 4096:
        raise SystemExit("Command exceeds 4096 bytes")
    try:
        print(json.dumps(handle(json.loads(frame, object_pairs_hook=unique_fields))))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
