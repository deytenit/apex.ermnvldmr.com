---
name: update-compositions
description: Use when updating, upgrading, or bumping Docker container images, tags, composition services, and OS packages across one or more APEX fleet nodes.
---

# Updating APEX Compositions & System Packages

This skill guides the safe, structured update of container images, compose definitions, and base operating system packages across nodes in an APEX fleet.

## Core Rules

1. **Verify access first**: Never execute commands against a node without confirming SSH connectivity and credentials.
2. **Hold critical services**: Always ask the user which services or compositions must NOT be touched before discovering or updating images.
3. **Classify update scope**: Separate updates into **Minor/Patch** (non-breaking), **Major** (breaking / schema migration risks), and **Unstable SemVer / CalVer** (requiring changelog scrutiny).
4. **Gate major and breaking updates**: Never apply a Major version bump, SemVer `0.x` minor change, or CalVer jump without migration research and explicit user approval.
5. **Preserve APEX engine context**: Always orchestrate compose actions through `./commons/apex compose` (or with derived `APEX_*` variables and layered env files) rather than bare `docker compose`.
6. **Safeguard in-tree patches**: Do not bump images for containers with host bind mounts that patch application source code without explicit review.
7. **Verify fleet observability post-rollout**: An update is not complete until containers reach `Up` and `healthy`, logs are clean, Prometheus scrape targets report `UP`, blackbox probes pass, and remote write streams are established.

---

## Workflow Steps

### Step 1: Confirm Node Access

1. Identify the target nodes from the user request.
2. If access credentials, custom SSH ports, or key paths are not declared in the environment's SSH configuration, confirm:
   - SSH hostname / IP
   - SSH port (APEX default: `2222`)
   - SSH user
   - Identity file / key
3. Test connectivity to each node before proceeding:
   ```bash
   ssh -p <port> <user>@<node_fqdn> "hostname && uptime"
   ```
4. **Completion check**: All target nodes return exit code 0 on the connectivity probe.

---

### Step 2: Inquire About Critical Services & Holds

1. Prompt the user explicitly before inspecting or modifying any services:
   > "Which critical services or compositions on these nodes should **NOT** be updated during this run (e.g. databases, SSO/Identity providers, Mail, Vaults, core routing)?"
2. Record the freeze list (held services/compositions).
3. Ensure all subsequent steps skip services on the freeze list.
4. **Completion check**: User response received, and the list of excluded services is confirmed.

---

### Step 3: Inventory Running Services, Images, and Custom Patches

For each target node:

1. List active containers, current images, and status:
   ```bash
   ssh <node> "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'"
   ```
2. Locate the compose files on the node:
   - Shared core: `/srv/<node_fqdn>/compositions/apex/docker-compose.yml`
   - Node applications: `/srv/<node_fqdn>/compositions/*/docker-compose.yml`
   - Overrides: `docker-compose.override.yml` in each composition directory
3. Map every running container to its compose declaration:
   - Image repository
   - Pinned tag / SHA digest
   - Whether the image is locally built (`build:` context present)
4. **Inspect for in-tree patches & mounts**:
   - Check container volume declarations for bind mounts into application code paths (e.g. `./patches/...:/app/...`, monkeypatched modules, or custom entrypoint scripts).
   - If a service mounts patches into internal software paths, flag it: upstream image updates may alter internal APIs and break the patch. Add to hold or require explicit user confirmation.
5. Exclude any services identified on the freeze list from Step 2.
6. **Completion check**: A complete table of candidate compositions, running tags, compose file paths, and in-tree patch status is constructed.

---

### Step 4: Seek Latest Tags and Classify Update Types

For each candidate service:

1. Query the container registry (Docker Hub, GitHub Container Registry `ghcr.io`, Quay, etc.) for available release tags. Refer to [references/registry-queries.md](references/registry-queries.md) for registry API patterns.
2. Determine the latest stable versioned tag:
   - Prefer semantic versioned tags matching the existing packaging flavor (e.g. `-alpine`, `-bookworm`).
   - Ignore pre-release, alpha, beta, or release-candidate (`-rc`) tags unless the running container already pins one.
   - Do not replace a pinned version tag with bare `:latest`.
3. Compare the current tag with the latest tag:

| Tag Transition | Category | Classification | Action Path |
| :--- | :--- | :--- | :--- |
| `1.37.1` -> `1.37.2` | SemVer `1.x+` | **Patch** | Safe -> Proceed to Step 6 |
| `1.37.1` -> `1.38.0` | SemVer `1.x+` | **Minor** | Safe -> Proceed to Step 6 |
| `0.4.1` -> `0.4.2` | SemVer `0.x` | **0.x Patch** | Safe -> Proceed to Step 6 |
| `0.4.1` -> `0.7.0` | SemVer `0.x` | **0.x Minor** | **Route to Step 5 (Strict Gate)** |
| `YY.M.D` -> `YY.M.D` | CalVer / Date | **CalVer Jump** | Multi-month jump -> **Route to Step 5** |
| Floating tag (e.g. `:4`, `:16`) | Floating | **In-place Digest Bump** | Safe -> Proceed to Step 6 |
| `1.x` -> `2.x` or `16` -> `17` | SemVer `1.x+` | **Major** | **Route to Step 5 (Strict Gate)** |
| Locally built image (`build:`) | Local Build | **Local Build** | Git update + build (Step 6) |

> [!IMPORTANT]
> **SemVer 0.x Rule**: Per SemVer specification Clause 4, version `0.x` software has no public API stability guarantees. Minor version bumps (e.g. `0.4` -> `0.7`) often contain breaking changes, renamed environment variables, or schema changes. Treat them with the same scrutiny as Major updates.
>
> **CalVer Rule**: Projects using date-based versioning (e.g. Xray, Ubuntu base images) do not follow SemVer breaking change rules. Releases spanning several months must be vetted against upstream changelogs for breaking configuration or default behavior shifts.

4. **Completion check**: Every eligible service has a determined target tag and update classification.

---

### Step 5: Major & Breaking Update Protocol (Strict Gating)

For each service classified as **Major**, **0.x Minor**, or **CalVer Jump**:

1. **Research Upstream Changes**:
   - Check upstream release notes, migration guides, and changelogs.
   - Identify:
     - Breaking configuration changes (renamed environment variables, CLI flags, config format alterations).
     - Altered default security behaviors (e.g. new access control rules, loopback/private IP blocking, required authentication headers).
     - Storage schema migrations (e.g., PostgreSQL major upgrade requirements).
2. **Formulate Migration & Rollback Plan**:
   - Outline pre-upgrade steps (e.g. explicit logical database dump `pg_dump` / `mysqldump` beyond normal snapshots).
   - Outline required compose file changes (volumes, environment variables).
   - Outline fallback/rollback steps if the update fails.
3. **Request Explicit User Approval**:
   - Present findings to the user:
     - Target service and node.
     - Current version vs New version.
     - Summary of breaking changes and migration strategy.
   - Ask: *"Do you approve proceeding with the update for `<service>` on `<node>` under this strategy?"*
4. **Gate**:
   - If approved: Proceed to Step 6 for this service.
   - If rejected or deferred: Add service to the freeze list and leave it untouched.
5. **Completion check**: Every breaking or major update has documented strategy + user approval or is explicitly skipped.

---

### Step 6: Apply Container Updates via APEX Engine

Follow node and service rollout order to minimize blast radius:
1. **Node ordering**:
   - Application & worker nodes first.
   - Communications, data, and storage nodes next.
   - Core edge / gateway / SSO nodes last, ensuring routing and auth remain stable during earlier rollouts.
2. **Service dependency sequencing**:
   - Infrastructure providers first (DNS resolvers, database servers, mail exchangers).
   - Verify provider health before updating or restarting dependent consumer services (e.g. SSO forwardAuth, telemetry agents).

For each node:

1. **Pre-upgrade backup**:
   - Trigger a backup of stateful volumes:
     ```bash
     ssh <node> "apex backup/run"
     ```
   - For database services being updated, execute an in-container logical export to host storage.
2. **Update image tags in compose definitions**:
   - Modify the target `docker-compose.yml`, `docker-compose.override.yml`, or node configuration repository.
3. **Pull and recreate services via APEX CLI**:
   - Always run through the node's APEX CLI to preserve derived `APEX_*` variables and layered env files:
     ```bash
     ssh <node> "cd /srv/<node_fqdn> && ./commons/apex compose pull"
     ssh <node> "cd /srv/<node_fqdn> && ./commons/apex compose up -d"
     ```
   - *Note on `--force-recreate`*: When updating containers using floating tags, recovering from host reboots, or handling services with `tmpfs` state (e.g. mail spools), pass `--force-recreate` to ensure the container entrypoint runs a clean initialization:
     ```bash
     ssh <node> "cd /srv/<node_fqdn> && ./commons/apex compose up -d --force-recreate"
     ```
4. **Completion check**: Compose commands finish with exit code 0.

---

### Step 7: Apply System Packages and OS Updates

When the update scope includes host operating system packages:

1. **Pre-upgrade check**:
   - Check available disk space: `df -h /`
   - Check for broken package states: `sudo dpkg --audit`
2. **Handle unattended package configuration (debconf)**:
   - Prevent interactive prompts (e.g. GRUB drive selection on VPS instances) from blocking non-interactive execution:
     ```bash
     # Example for grub-pc on virtualized hosts using /dev/sda:
     echo 'grub-pc grub-pc/install_devices multiselect /dev/sda' | sudo debconf-set-selections
     ```
3. **Execute system package upgrade**:
   ```bash
   ssh <node> "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y"
   ```
4. **Clean package cache**:
   ```bash
   ssh <node> "sudo apt-get autoremove -y && sudo apt-get clean"
   ```
5. **Reboot handling & coordination**:
   - Check if a system reboot is required:
     ```bash
     ssh <node> "test -f /var/run/reboot-required && echo 'REBOOT_REQUIRED' || echo 'OK'"
     ```
   - If reboot is required:
     1. Notify the user before initiating the reboot.
     2. Reboot node: `ssh <node> "sudo reboot"`
     3. Wait for SSH availability: poll connectivity with timeout.
     4. After boot, verify systemd state (`systemctl is-system-running`) and check that Docker containers auto-started.
     5. If services with `tmpfs` mounts fail startup checks (e.g. Postfix spool directories in mail containers), run `./commons/apex compose up -d --force-recreate`.
6. **Completion check**: Host packages are up to date and node is online.

---

### Step 8: Verify Service Health & Fleet Observability

Immediately after recreation and host reboots on each node:

1. **Inspect container state**:
   ```bash
   ssh <node> "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
   ```
   - Verify status is `Up`.
   - Verify healthchecks transition to `(healthy)` and do not hang in `(unhealthy)` or `(starting)`.
2. **Inspect recent logs**:
   ```bash
   ssh <node> "docker logs --tail 50 <container_name>"
   ```
   - Check for crash loops, panic traces, database connection errors, or authentication failures.
3. **Verify HTTP/network response (edge/web services)**:
   ```bash
   curl -IfsS -k https://<service_fqdn> || true
   ```
4. **Observability & Telemetry Verification**:
   - If the fleet runs Prometheus:
     - Check scrape targets: `curl -s http://<prom_internal_ip>:9090/prometheus/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) | \(.health)"'` — verify all targets report `up`.
     - Check blackbox probes: `curl -s 'http://<prom_internal_ip>:9090/prometheus/api/v1/query?query=probe_success==0'` — verify 0 failing probes.
     - Check active alerts: `curl -s http://<prom_internal_ip>:9090/prometheus/api/v1/alerts | jq '.data.alerts[] | select(.state=="firing")'` — verify only expected watchdogs fire.
     - Check remote write: confirm telemetry agent (e.g. Alloy) is actively shipping host/container metrics without push retry errors.
5. **Rollback protocol**:
   - If a container enters a crash loop (`Restarting`, `Exit 1`) or breaks ingress:
     1. Capture failure logs.
     2. Revert the image tag in the compose file to the prior tag.
     3. Re-run `./commons/apex compose up -d --force-recreate`.
     4. Notify user immediately with logs and root cause.
6. **Completion check**: All containers are confirmed `Up` and `healthy`, external ingress functions normally, and telemetry metrics/targets are green.
