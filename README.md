# Sovereign Linux on Fedora CoreOS

Build a disposable x86-64 QEMU machine from Fedora CoreOS stable `44.20261023.3.1`, the release used in the original runner lab. The build produces a fresh QCOW2 disk and a first-boot Ignition file. Keep the two together. The configuration sets the hostname to `sovereign-linux` and prints `SOVEREIGN_LINUX_BASE_READY` on the guest console after checking the hostname file and invoking the guest's Podman binary.

The base runs offline. For an attended n8n test, build the runner profile below. Owner pairing and chat delivery remain separate work. Shirudo is optional across both profiles.

## Build locally

Open a terminal at the root of this checkout. Building requires Bash, GNU coreutils, QEMU's `qemu-img`, Butane, and room for a roughly 2 GB disk copy. KVM is only relevant when booting. All generated files go under the ignored `build/` directory.

**1. Prepare Butane.** The prepared workstation checkout already has the verified compiler at `build/tools/butane`. On a fresh x86-64 Linux checkout, run the following once. To use a separately installed compiler, set `BUTANE` to its absolute path. This build was tested with [Butane v0.29.0](https://github.com/coreos/butane/releases/tag/v0.29.0).

```bash
(
  set -eu
  mkdir -p build/tools
  tmp=$(mktemp build/tools/butane.XXXXXXXX)
  trap 'rm -f -- "$tmp"' EXIT
  curl -fL -o "$tmp" https://github.com/coreos/butane/releases/download/v0.29.0/butane-x86_64-unknown-linux-gnu
  echo "53a20d820fbaa7fda4f1afd1814974badc8e448db4c151d3f1ba005dc29bc4c9  $tmp" | sha256sum -c -
  chmod 0755 "$tmp"
  mv -T -- "$tmp" build/tools/butane
)
```

**2. Select the base disk.** If you already have the original uncompressed Fedora QCOW2 for this release, set `base` to its absolute path and skip the download. The build verifies the disk copy against the pinned SHA-256. To fetch the same base on a fresh machine, run the following from the checkout. This path also needs `curl` and `xz`.

```bash
mkdir -p build
cache=$(mktemp -d build/fedora.XXXXXXXX)
base="$PWD/$cache/coreos.qcow2"
(
  set -eu
  cd "$cache"
  release=44.20261023.3.1
  url="https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/$release/x86_64/fedora-coreos-$release-qemu.x86_64.qcow2.xz"
  curl -fL -o coreos.qcow2.xz "$url"
  echo 'c759290b2c7f25dd304d55bfc67077e1bcc44ed82681d7db81c3abf53e873b0f  coreos.qcow2.xz' | sha256sum -c -
  xz --decompress --keep coreos.qcow2.xz
)
```

**3. Build the machine.** Run both lines in the same shell. Each successful build prints a new directory. The input disk is opened for reading only.

```bash
machine=$(bash scripts/build-local.sh "$base") &&
  (cd "$machine" && sha256sum -c SHA256SUMS)
```

Edit `machine/sovereign-linux.bu` and repeat step 3 to build another machine. Butane's strict mode checks the configuration before the disk copy. The builder checks the disk digest and QCOW2 structure before returning its output path. [Fedora CoreOS configuration specification](https://coreos.github.io/butane/config-fcos-v1_6/)

## Boot check for a later attended run

From the same terminal, use the following command when ready to boot. QEMU uses a temporary disk overlay, a serial console, two virtual CPUs, 2 GiB of RAM, and no network interface. The five-minute timeout ends the run if left unattended. Software emulation (`tcg`) works without a KVM device and can be slow. Where KVM is accessible, substitute `-accel kvm`.

```bash
(
  set -eu
  cd "${machine:?Set machine to the completed build directory}"
  sha256sum -c SHA256SUMS
  timeout --foreground --kill-after=10s 5m qemu-system-x86_64 \
    -machine q35 -accel tcg -smp 2 -m 2048 \
    -drive if=virtio,format=qcow2,file=machine.qcow2,snapshot=on \
    -fw_cfg name=opt/com.coreos/config,file=machine.ign \
    -nic none -display none -monitor none -no-reboot \
    -chardev stdio,id=console,signal=on -serial chardev:console
)
```

Look for the Podman version followed by `SOVEREIGN_LINUX_BASE_READY` on the serial console. Press Ctrl+C to stop QEMU. A login prompt or timeout alone does not establish that the check passed. This marker covers the base configuration only. The historical n8n result of `42` has its own runner and workflow prerequisites.

## Build and test the n8n runner

Use the existing lab's `runner-image.tar` with the same uncompressed Fedora disk used above. The second build argument adds a read-only ISO containing the exact JavaScript runner archive and its guest start script. The guest mounts the ISO at `/var/mnt/runner`. Build prerequisites for this profile include `xorriso` and `jq`.

```bash
machine=$(bash scripts/build-local.sh "$base" /path/to/runner-image.tar) &&
  (cd "$machine" && sha256sum -c SHA256SUMS)
```

For one attended test on Linux, the host needs Python 3, rootless Podman, QEMU, OpenSSH and the locally available lab main image `0fc7fd41adb46f4b70f1c8501325aab91f28363df6d2dd567720372def7037c2`. The harness starts a fresh main on an internal network. Only its broker port is published on host loopback; the harness calls the main API from inside the container. It boots the guest from a temporary overlay, pins a per-run SSH host key and connects the guest to the broker over a restricted QEMU network. The guest checks the runner archive and image ID before starting the unprivileged container. Session keys and the broker token are generated locally for each run.

```bash
python3 scripts/smoke-runner.py "$machine" build/my-first-run-evidence
```

The evidence path must be new. The harness records dispatch intent before submitting the inactive fixture's manual trigger. If a submitted run is interrupted, use its recorded workflow and execution IDs to inspect the saved result before considering another run. Choosing a new directory requests a new execution.

### Inspect host resources after an interruption

Before creating the rootless network or main container, the harness saves `resource-intent.json` in the private request directory. Both objects carry the same request ID and a fresh attempt label. After creating the network, the harness verifies its name and both labels, then uses its full network ID for container attachment and cleanup. If ownership or a creation-specific ID cannot be verified, startup stops and the intent remains available for inspection. From the same rootless Podman account and store, inspect the original evidence directory with:

```bash
python3 -I scripts/smoke-runner.py --inspect-resources "$evidence"
```

Set `evidence` to the original request directory. The JSON snapshot reports each resource as `owned` with its full Podman ID when the generated name and both labels match, `foreign` for a label mismatch, `absent` for a negative existence check, or `unknown` for an inconclusive lookup. Older evidence without an ownership record and invalid records produce `unknown` before Podman is queried. The command exits zero when both lookups return `owned` or `absent`; other outcomes exit nonzero.

Host Podman calls use local mode. The intent records a fingerprint of the machine, effective UID, graph-root path and directory identity, run root, storage driver and network backend. Inspection checks the fingerprint before and after reading the inventory. A different store, an unavailable store, or older evidence without the fingerprint yields `unknown`. Cleanup also checks the store before removing the verified network ID.

Inspection reads the current inventory. A pending create can still finish after an `absent` observation. Keep the worker reservation and use the existing dispatch and execution receipts to determine workflow status. The inspector never performs cleanup, dispatches work or changes the ledger. The labels identify runs within the trusted rootless account; anyone with control of that account can copy them.

A passing `result.json` records one execution, one Code output item, the fixture marker, and integer `42`. The harness compares the execution API response with the stopped main's SQLite database. It also records VM and main shutdown. The gitignored private database directory is identified in `dispatch-intent.json`. It contains synthetic account and session data and should stay local.

## Local Sovereign Tasks binding

The worker adapter in `bindings/sovereign_smoke.py` implements the fixed `sovereign-linux-n8n-smoke` profile for `ops-laptop-03`. It checks the harness and workflow against source commit `3266754586355ffd3fa85f82d06ef28b0e319a5f` and pins the verified bundle's manifest to `1c7197b0492db12f0f7c668c2ab05595ed0abac2a8f6722b487c268142dbf428`. The harness validates the disk, Ignition file, and runner ISO before boot.

The adapter's `broker_command` maps `run_sovereign_linux_smoke` and `read_sovereign_linux_smoke` to worker operations. Each takes only `request_id` from the owner. The authenticated broker supplies the device and the original claim. The worker reads one bounded JSON command on stdin and returns a JSON response. No socket listener, MCP registration, or service installation is included.

On first delivery the worker commits the request and claim to a private SQLite ledger before launching the fixed harness. The harness runs independently of the broker response timeout. Keep the worker's process group alive through boot and cleanup when configuring service supervision. Poll with the same request and claim. The ledger lives under `~/.local/state/sovereign-tasks/sovereign-linux-n8n-smoke/` with per-request evidence and a private log. Preserve that state across worker restarts.

| Worker state | Meaning |
| --- | --- |
| `not_found` | A status read found no reservation. |
| `running` | The worker log is locked by the running process and no execution ID has been verified yet. |
| `submitted` | The request evidence contains the workflow and execution IDs while the worker is running. |
| `uncertain` | The reservation exists but the evidence is incomplete or inconsistent. Re-read the same request. A new run is blocked pending reconciliation. |
| `complete` | Both saved-result readbacks and the shutdown record agree. Subsequent reads and duplicate runs return the stored receipt. |

Each fresh n8n database may start with execution ID `1`. Use the request, claim, workflow, and execution identifiers together. Duplicate delivery of another claim for a reserved request is refused.

Run the offline checks from the checkout root:

```bash
python3 -B -m unittest discover -s tests -v
```

For live integration the owner-facing tools and broker must admit this profile separately from the legacy lab. Route its claimed commands to this adapter under the intended worker identity. Status reads must reuse the original claim, including before an execution ID exists. The broker must accept the states above and scope execution IDs to their request. Live activation is a separate operator action.
