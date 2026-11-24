# Four local source patches

Base: `22f2ad0ab2ecc0a008cd209eb76d758af719af83` on `feat/fcos-n8n-runner`.

Apply the numbered patches in order. Each patch changes only the source and tests named below.

1. `01-host-rootless.patch` checks the host Podman identity before creating evidence or resources.
2. `02-private-main-api.patch` keeps the fresh main API inside its container and sends owner setup data over `podman exec` stdin.
3. `03-sigterm-cleanup.patch` lets a SIGTERM unwind the cleanup stack and write the shutdown record.
4. `04-poll-response-and-pins.patch` validates poll responses and report acknowledgements, updates the pinned runner revision and binding hash, and corrects the README.

The fourth patch pins the runner to commit `1ba5f6057181f95fc43c9368cb0350b15259cd1a`. Preserve this commit history when transferring the series. Applying the text patches without that Git object makes the worker's source check fail closed.

`exact-four-commits.bundle` carries the four commits with their exact object IDs. It requires the base commit above and passed `git bundle verify` in this checkout.

`artifact-hashes.sha256` records the four patch files and the Git bundle. It passed `sha256sum -c` from the repository root.

These patches were checked with offline unit tests and a replay against the base tree. They do not establish a successful VM smoke, a live broker admission, or deployment approval.

Before an attended run, refresh and repin the n8n main and Fedora CoreOS base images, admit the new revision in the broker, and define service supervision plus reconciliation for hard kills or a timed-out Podman create that may have left a resource. Those are separate build and operations steps.
