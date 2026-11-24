# Sovereign Linux: AWS base candidate

Status: source implementation and offline packaging. No AWS resources are created by this builder. This candidate reuses the existing Fedora CoreOS base configuration; it does not include the QEMU runner ISO, n8n activation, task-worker enrollment, customer credentials, or a desktop.

## Direction and first release

Sovereign Linux is an optional, reproducible engineering environment. An AWS launch target is its first cloud packaging route. Academy and Sovereign Tasks remain independently usable. A user's machine identity, access grants and private state belong to that user and instance. Reviewed knowledge can be distributed separately as versioned data; receiving it does not authorize execution.

For the first AWS base proof, reuse an official FCOS AMI with the existing Butane configuration. This produces a repeatable launch package. It does not create an owned, custom-baked AMI. Consider a custom image build only when a measured provisioning requirement or a concrete component requires it. Preserve the build recipe as the source of truth.

| Stage | Deliverable | Acceptance |
| --- | --- | --- |
| 1. Prepare the base | Exact AMI selection, pinned compiler, Ignition and launch data | Offline tests pass; hashes and preparation state retained |
| 2. Attended AWS base proof | Two disposable instances, one at a time, from the same package | Each console emits the existing base marker; control-plane settings and cleanup are observed |
| 3. Owner-controlled environment | Explicit access and fresh machine identity, with one selected tool profile | Owner can use the tools; distinct guest identities and scoped access are checked |
| 4. Useful work | One separately authorized task and retained result | Dispatch, execution, report and independent review remain distinct |
| 5. Distributable release | Versioned recipe and delivery instructions; optional custom AMI if justified | Clean launch, owner pairing, update and rollback behavior demonstrated |

Stage 1 is the scope implemented here. Stages 2–5 require their own observed results; the table is a plan.

## Selected baseline

| Field | Value |
| --- | --- |
| FCOS release / architecture | `44.20261023.3.1` / `x86_64` |
| Region | `eu-west-1` (Ireland) |
| AMI | `ami-0d094a1da6f4cdb56` |
| Expected publisher account | `125523088429` |
| Root device / source size | `/dev/xvda` / 10 GiB |
| Source configuration | `machine/sovereign-linux.bu` |
| Base source commit | `7cd939f43a33c5fd244abdcf07a607168d285fe5` |
| Butane | `0.29.0`, SHA-256 `53a20d820fbaa7fda4f1afd1814974badc8e448db4c151d3f1ba005dc29bc4c9` |

AWS `DescribeImages` on 24 November 2026 reported this exact AMI as available, public, owned by the expected publisher, and named `fedora-coreos-44.20261023.3.1-x86_64`. Its complete mapping had one 10 GiB EBS root and an `ephemeral0` declaration. The allowed T3 instance types have EBS storage only. Recheck the full image mapping before a real launch; extra EBS mappings must be refused or explicitly suppressed. A root override alone does not remove inherited devices.

The publisher's [release metadata](https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/44.20261023.3.1/x86_64/meta.json), retrieved on the same date, independently maps this exact release and architecture to the same Ireland AMI and snapshot. The retrieved metadata SHA-256 was `0b001d60a1408393e7544fa2081fc2a22e25c09dabc01677e44db5fd9ff65ed3`.

The selected release preserves the existing local base for an equivalence check. It is not a claim that this is the latest FCOS release or a completed current security review. The AMI pin fixes the starting image; FCOS's later OS-update behavior needs an explicit operating policy before long-lived use.

## Prepare a package locally

Use the [root README](../README.md#build-locally) to obtain the checksum-pinned Butane compiler. The AWS packager needs Python 3 and that compiler; it does not need a QCOW2 disk, QEMU, AWS credentials, an AWS SDK or an AWS CLI.

For an offline demonstration using deliberately synthetic subnet and security-group identifiers:

```bash
python3 -B scripts/build-aws.py \
  --config aws/base.example.json \
  --butane build/tools/butane
```

The example's zero-valued resource IDs are placeholders. They are not verified resources or approved launch settings. For a real candidate, copy this non-secret JSON into `build/` and substitute the chosen subnet and security-group IDs after checking their rules and route. Keep real deployment inputs under ignored `build/`, not in the public repository.

`key_name` is an existing AWS key-pair **name**, or `null` for a headless base check. Never put a private key or token in this file. `associate_public_ip_address` must be an explicit boolean. The profile allows only `t3.small` or `t3.medium` and a 10–64 GiB root disk. The example selects 16 GiB.

Each successful invocation creates a new private `build/aws.*` directory containing:

- `machine.ign`: the exact base configuration compiled by the verified Butane binary, limited to 16,384 raw bytes.
- `launch-template-data.json`: a **fresh, standalone** `LaunchTemplateData` object with base64-encoded user data.
- `manifest.json`: compiler, source, settings and output hashes, plus explicit `prepared` / `not_created` / `not_launched` states.

The builder checks the compiler bytes before running a private copy. It pins the base configuration and restricts the compiled result to the existing hostname file and base-check service. It refuses extra input fields, changed base intent, remote Ignition configuration and additional users or commands. It performs no networking and does not print input values on a validation error.

The manifest's supplied `source_commit` is declared provenance, not Git verification. The configuration bytes and packager have their own hashes. Match the declaration to a real checkout when preparing a release. Hashes establish byte correspondence; the manifest is not a signed attestation or evidence of cloud execution.

## Launch controls and preflight

The generated settings require IMDSv2, keep the metadata hop limit at 1, disable metadata tags, select encrypted `gp3` with deletion on termination, and use standard T3 CPU credits. Network placement and public-IP allocation are explicit. No IAM instance profile is emitted. These controls are proposals until AWS reports the actual instance configuration.

Use the object to create a **new** dedicated template, if a template is needed. Do not pass it as an inherited update using `CreateLaunchTemplateVersion(SourceVersion=...)`: omitted fields could be inherited, including an instance profile. Pin the resulting numeric template version for a launch. Never infer approval from possession of a generated file.

Before any cloud mutation, verify the intended account and operator identity, AMI owner/state/mappings, instance-type availability, selected subnet and security-group relationship, management route, access method, cost window and cleanup scope. The offline packager cannot verify those live conditions. Security-group IDs alone do not establish isolation.

The first proposed attended test is a headless base check: two `t3.small` instances in sequence, no public IPv4, no key pair, no instance role and a dedicated group with no inbound or outbound rules. Keep the window bounded and retain each original instance ID before any follow-up. Reconcile uncertain API results before considering another launch. No warm pool, NAT gateway, load balancer, recurring pipeline or worker admission is needed for that test.

For this headless profile, the guest's existing `SOVEREIGN_LINUX_BASE_READY` marker proves only the hostname-file check and a successful Podman invocation. Collect console output and control-plane evidence, then terminate the exact test instances, verify attached test EBS volumes are gone, and remove the test group. Do not modify the default security group.

Separate EC2 instance IDs and EBS volume IDs can be observed in this test. Guest machine-ID uniqueness, SSH-host-key uniqueness, rootless application operation, owner pairing, knowledge access and agent execution are **not tested** by this marker. Those belong to the next attended acceptance with a reviewed observation/access method.

## Verification

```bash
python3 -B -m unittest discover -s tests -v
```

The AWS tests exercise rejection paths, checksum-before-execution, fixed launch controls, private outputs, no overwrite, size limits and the real pinned compiler when it is present. A skipped real-compiler test means actual compilation remains unverified. Passing offline tests is not an AWS boot result.

## Sources

- [FCOS AWS provisioning](https://github.com/coreos/fedora-coreos-docs/blob/main/modules/ROOT/pages/provisioning-aws.adoc): Ignition user data, Afterburn SSH-key handling and the 16 KB limit.
- [FCOS configuration](https://github.com/coreos/fedora-coreos-docs/blob/main/modules/ROOT/pages/producing-ign.adoc): Butane compilation and first-boot provisioning.
- [Launch template data](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RequestLaunchTemplateData.html).
- [Launch template metadata](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_LaunchTemplateInstanceMetadataOptionsRequest.html).
- [Block-device inheritance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-block-device-mapping.html).
- [Template version inheritance](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateLaunchTemplateVersion.html).
