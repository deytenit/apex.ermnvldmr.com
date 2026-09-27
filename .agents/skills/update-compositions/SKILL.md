---
name: update-compositions
description: Use when updating, upgrading, or bumping Docker container images, tags, and composition services across one or more APEX fleet nodes.
---

# Updating APEX Compositions

This skill guides the safe, structured update of container images and compose definitions across nodes in the APEX fleet.

## Core Rules

1. **Verify access first**: Never execute commands against a node without confirming SSH connectivity and credentials.
2. **Hold critical services**: Always ask the user which services or compositions must NOT be touched before discovering or updating images.
3. **Classify update scope**: Separate updates into **Minor/Patch** (non-breaking) and **Major** (breaking / schema migration risks).
4. **Gate major updates**: Never apply a Major version bump without in-depth migration research and explicit user approval.
5. **Verify health post-recreation**: An update is not complete until containers reach `Up` and `healthy` status with clean logs.

---

## Workflow Steps

### Step 1: Confirm Node Access

1. Identify the target nodes from the user request (e.g., `icarus`, `daedalus`, `morpheus`).
2. If access credentials, custom SSH ports, or key paths are not declared in the environment's SSH configuration, ask the user:
   - SSH hostname / IP
   - SSH port (APEX default: `2222`)
   - SSH user (APEX default: `adam`)
   - Identity file / key
3. Test connectivity to each node before proceeding:
   ```bash
   ssh -p <port> <user>@<node_fqdn> "hostname && uptime"
   ```
4. **Completion check**: All target nodes return exit code 0 on the connectivity probe.

---

### Step 2: Inquire About Critical Services & Holds

1. Prompt the user explicitly before inspecting or modifying any services:
   > "Which critical services or compositions on these nodes should **NOT** be updated during this run (e.g. databases, SSO/Authelia, Mail, Vaultwarden, Matrix)?"
2. Record the freeze list (held services/compositions).
3. Ensure all subsequent steps skip services on the freeze list.
4. **Completion check**: User response received, and the list of excluded services is confirmed.

---

### Step 3: Inventory Running Services and Images

For each target node:

1. List active containers, current images, and status:
   ```bash
   ssh <node> "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'"
   ```
2. Locate the compose files on the node:
   - Shared core: `/srv/<node_fqdn>/compositions/apex/docker-compose.yml`
   - Node applications: `/srv/<node_fqdn>/compositions/*/docker-compose.yml`
   - Overrides: `docker-compose.override.yml` in each composition folder
3. Map every running container to its compose declaration:
   - Image repository (e.g. `authelia/authelia`, `postgres`, `jellyfin/jellyfin`)
   - Pinned tag / SHA digest (e.g. `:4`, `:16-alpine`, `:v1.37.1`)
   - Whether the image is locally built (`build:` context present)
4. Exclude any services identified on the freeze list from Step 2.
5. **Completion check**: A complete table of non-held compositions, running tags, and compose file locations is constructed.

---

### Step 4: Seek Latest Tags and Classify Update Types

For each candidate service:

1. Query the container registry (Docker Hub, GitHub Container Registry `ghcr.io`, Quay, etc.) for available release tags. Refer to [references/registry-queries.md](references/registry-queries.md) for registry API patterns.
2. Determine the latest stable versioned tag:
   - Prefer semantic versioned tags (e.g. `1.38.0`, `16.4-alpine`) matching the existing packaging flavor.
   - Ignore pre-release, alpha, beta, or release-candidate (`-rc`) tags unless the running container already pins one.
   - Do not replace a pinned version tag with bare `:latest`.
3. Compare the current tag with the latest tag using Semantic Versioning (SemVer: `MAJOR.MINOR.PATCH`):

| Tag Transition | Classification | Action Path |
| :--- | :--- | :--- |
| `1.37.1` -> `1.37.2` | **Patch** | Proceed to Step 6 |
| `1.37.1` -> `1.38.0` | **Minor** | Proceed to Step 6 |
| Floating tag (e.g. `:4` or `:16`) with new digest | **Minor / In-place** | Proceed to Step 6 |
| `16` -> `17` or `1.x` -> `2.x` | **Major** | **Route to Step 5 (Strict Gate)** |
| Locally built image (`build:`) | **Local Build** | Git update + build (Step 6) |

4. **Completion check**: Every eligible service has a determined target tag and is classified as Major or Minor/Patch.

---

### Step 5: Major Update Protocol (Strict Gating)

For each service classified as **Major**:

1. **Research Update Strategy**:
   - Check upstream release notes, migration guides, and changelogs.
   - Identify:
     - Breaking configuration changes (renamed environment variables, CLI flags).
     - Storage schema migrations (e.g., PostgreSQL major version pg_upgrade requirements).
     - Deprecated dependencies or protocol changes.
2. **Formulate Migration Plan**:
   - Outline pre-upgrade steps (e.g., explicit database dump `pg_dump` / `mysqldump` beyond normal snapshots).
   - Outline required compose file changes (volumes, environment).
   - Outline fallback/rollback steps if the upgrade fails.
3. **Request Explicit User Approval**:
   - Present the findings to the user:
     - Target service and node.
     - Current version vs New major version.
     - Summary of breaking changes and migration strategy.
   - Ask: *"Do you approve proceeding with the major update for `<service>` on `<node>` under this strategy?"*
4. **Gate**:
   - If approved: Proceed to Step 6 for this service.
   - If rejected or deferred: Add service to the freeze list and leave it untouched.
5. **Completion check**: Every Major update either has documented strategy + user approval or is explicitly skipped.

---

### Step 6: Apply Updates and Recreate Containers

Follow node rollout order to minimize blast radius:
1. Application nodes / workers first (e.g., `icarus`).
2. Communications / data nodes next (e.g., `morpheus`).
3. Core edge / gateway / SSO nodes last (e.g., `daedalus`), ensuring routing and auth remain stable during earlier rollouts.

For each node:

1. **Pre-upgrade backup**:
   - Trigger a backup of stateful volumes:
     ```bash
     ssh <node> "apex backup/run"
     ```
   - For database services being updated, execute an in-container logical export to host storage.
2. **Update image tags in compose files**:
   - Modify the target `docker-compose.yml` or `docker-compose.override.yml` with the new version tag.
3. **Pull images**:
   ```bash
   ssh <node> "cd /srv/<node_fqdn>/compositions/<service> && docker compose pull"
   ```
4. **Recreate services**:
   ```bash
   ssh <node> "cd /srv/<node_fqdn>/compositions/<service> && docker compose up -d"
   ```
5. **Completion check**: Compose commands finish with exit code 0.

---

### Step 7: Verify Service Health & Stability

Immediately after recreation on each node:

1. **Inspect container state**:
   ```bash
   ssh <node> "docker ps -a --filter name=<container_name> --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
   ```
   - Verify status is `Up`.
   - If a healthcheck is configured, verify it transitions to `(healthy)` and does NOT get stuck in `(unhealthy)` or `(starting)`.
2. **Inspect recent logs**:
   ```bash
   ssh <node> "docker logs --tail 50 <container_name>"
   ```
   - Check for crash loops, panic traces, connection refused, or migration errors.
3. **Verify HTTP/network response (if edge/web service)**:
   ```bash
   curl -IfsS -k https://<service_fqdn> || true
   ```
4. **Rollback protocol**:
   - If a container enters a crash loop (`Restarting`, `Exit 1`):
     1. Capture failure logs.
     2. Revert the image tag in `docker-compose.yml` to the prior tag.
     3. Re-run `docker compose up -d`.
     4. Notify user immediately with logs and root cause.
5. **Completion check**: All updated containers are confirmed `Up` and `healthy` with clean logs.
