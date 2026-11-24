"""Outbound worker for the dedicated Sovereign Linux smoke profile."""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import runpy
import secrets
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

SITE = "https://sovereign-tasks.vaultsovereign.chatgpt.site"
DEVICE = "ops-laptop-03"
PROFILE = "sovereign-linux-n8n-smoke"
BINDING_HASH = "2abe8e59d9ec54b041e5e3182b45c60feaf03c20d07233bc40956bc65776ae1f"
PROBE_ID = "smoke_" + "0" * 32


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Transport:
    def __init__(self, credentials):
        self.key = serialization.load_pem_private_key((credentials / "signer.pem").read_bytes(), password=None)
        self.ingress = (credentials / "site-ingress").read_text().strip()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def post(self, action, payload):
        if action not in ("poll", "report"):
            raise ValueError("Unknown smoke endpoint")
        path = "/linux-smoke/" + action
        body = json.dumps(payload, separators=(",", ":")).encode()
        stamp, nonce = str(int(time.time())), secrets.token_hex(20)
        canonical = "\n".join([stamp, nonce, "POST", path, hashlib.sha256(body).hexdigest()])
        der = self.key.sign(canonical.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        sig = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode().rstrip("=")
        request = Request(SITE + path, data=body, headers={
            "Content-Type": "application/json", "OAI-Sites-Authorization": "Bearer " + self.ingress,
            "X-Sovereign-Timestamp": stamp, "X-Sovereign-Nonce": nonce, "X-Sovereign-Signature": sig,
        })
        with self.opener.open(request, timeout=15) as response:
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("Oversized response")
            return json.loads(raw)


def tick(binding, post):
    handle = binding["handle"]
    probe = handle(dict(profile=PROFILE, device_id=DEVICE, operation="status",
                        request_id=PROBE_ID, claim_id="claim_" + "0" * 32))
    if probe["state"] != "not_found":
        raise ValueError("Reserved read probe has local state")
    answer = post("poll", dict(profile=PROFILE, device_id=DEVICE, revision=binding["REVISION"],
                               manifest=binding["MANIFEST"], probe="not_found"))
    if (not isinstance(answer, dict) or set(answer) != {"command", "probe"}
            or answer["probe"] != "not_found"):
        raise ValueError("Unexpected signed-poll response")
    cmd = answer["command"]
    if cmd is None:
        return False
    binding["validate"](cmd)
    if cmd["request_id"] == PROBE_ID:
        raise ValueError("Reserved probe cannot be dispatched")
    result = handle(cmd)
    if result["state"] == "not_found":
        # An interrupted claim can lose its first delivery. Never convert status to run.
        result["state"] = "uncertain"
    acknowledgement = post("report", result)
    if (not isinstance(acknowledgement, dict) or set(acknowledgement) != {"accepted"}
            or acknowledgement["accepted"] is not True):
        raise ValueError("Report unacknowledged")
    return True


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    src = Path(__file__).with_name("sovereign_smoke.py")
    if hashlib.sha256(src.read_bytes()).hexdigest() != BINDING_HASH:
        raise SystemExit("Local binding digest mismatch")
    binding = runpy.run_path(str(src))
    binding["check_target"]()
    transport = Transport(Path(os.environ["CREDENTIALS_DIRECTORY"]))
    failures = 0
    ready = False
    while True:
        try:
            busy = tick(binding, transport.post)
            if not ready:
                logging.info("Dedicated signed poll and local read probe verified.")
            ready, failures = True, 0
            time.sleep(3 if busy else 20)
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            ready = False
            failures += 1
            code = error.code if isinstance(error, HTTPError) else type(error).__name__
            logging.warning("Dedicated poll unavailable (%s).", code)
            time.sleep(min(60, 5 * failures))


if __name__ == "__main__":
    main()
