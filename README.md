<p align="center">
  <img alt="apex.ermnvldmr.com logo" src="./assets/logo.png" height="128" />
</p>

# [apex.ermnvldmr.com](https://apex.ermnvldmr.com)

Local infrastructure operations for independently owned nodes, with a Python standard-library core.

## Quick Start

- **Live Documentation**: <https://docs.ermnvldmr.com/en/apex/> (also available in [Russian](https://docs.ermnvldmr.com/ru/apex/))
- **Getting Started Guide**: <https://docs.ermnvldmr.com/en/apex/getting-started/>
- **Report an Issue**: <https://github.com/deytenit/apex.ermnvldmr.com/issues>
- **License**: [BSD-2-Clause](#copyright--license)

```sh
# Clone your node's repository (which pins apex as commons/)
git clone --recurse-submodules git@github.com:you/selfhost.example.com.git
cd selfhost.example.com && ./init.sh && source ~/.bashrc

# Configure node identity
cat > node.env <<'EOF'
APEX_NODE_FQDN=selfhost.example.com
APEX_NODE_HOST=selfhost.example.com
APEX_SUBNET=198.18.0.0/24
EOF

# Configure host baseline and bring up compositions
apex configure        # firewall, crowdsec, cron, systemd, routing
apex compose up       # bring node services online
```

## Essential Information

You should understand the following invariants before exploring the codebase:

### Core Principles

- **No Control Plane**: No central orchestrator, agent daemon, or cluster controller. Commands act locally on the node you run them on.
- **Core Runtime**: Python 3.10 or newer and Git, with no `pip` dependencies or build step. Standalone host provisioning and workstation checks have separate requirements below.
- **Node-Owned Repositories**: Each machine is an independent Git repository pinning `apex` as a `commons/` submodule. It declares its own identity, compositions, and local overrides.
- **Overlay Architecture**: Node repositories can extend or override any shared action by placing an executable in `proprietaries/actions/`.

### Repository Structure

- **[`actions/`](./actions)**: Built-in engine actions (`compose.py`, `configure/`, `sync/`, `tiers/`, `backup/`, `utils/`).
- **[`compositions/`](./compositions)**: The shared core Compose stack (`traefik`, `socket-proxy`, `alloy`, `adguard`, `xray`, `restic`).
- **[`engine/`](./engine)**: Framework engine — action discovery, overlay resolution, context injection, and logging.
- **[`etc/`](./etc)**: Shared static configuration, including Alloy.
- **[`ci/`](./ci)**: Host image build recipes, cloud-init templates, and standalone Debian enrollment tooling.
- **[`assets/`](./assets)**: Repository branding assets (`logo.png`).

---

## Who Are You?

### Node Operator

Running and maintaining services on an existing host.

- **Requirements**: Debian-based host with Docker, Docker Compose v2, Git, and Python 3.
- **Workflow**:
  - Update services: `apex compose pull && apex compose up`
  - Run backups: `apex backup/run`
  - Sync repository state: `apex sync/repository`

Target a composition explicitly when operating on one application:

```sh
apex compose -p apex restart traefik
apex compose -p app up -d
apex compose --dry-run -p apex logs --tail 20 xray
```

Wrapper options precede the action; remaining arguments go to Docker Compose.
The positional forms `apex compose restart apex traefik` and `apex compose up -d apex`
remain supported. Use `-p` when service names or option values could be mistaken
for composition names. Without a target, APEX visits core first, then other
compositions alphabetically; `down` reverses that order.

Replace `app` with a composition defined in your repository. Use targeted `up -d`
after changing a container definition; `restart` retains the existing definition.

For direct Docker Compose use in the core composition:

```sh
apex env --sync
cd compositions/apex
docker compose config --quiet
docker compose restart traefik
```

Sync preserves existing `.env` credentials and UID/GID entries, including tier
symlinks. It updates a managed block containing `apex.env` settings (including
profiles) followed by synthesized node/path values, and writes with mode `0600`.
Resync after changing `apex.env`, node identity, or tier paths. Shell environment
values still take precedence in Docker Compose. Do not edit the generated block.

`eval "$(apex env)"` exports synthesized APEX values to the current shell;
`apex env --dotenv` prints dotenv syntax, and `apex env --write generated.env`
writes those values to a separate file. These three modes do not merge the
composition's secrets or `apex.env`; use `--sync` for the complete core bridge.
Choose one output mode per invocation.

### Infrastructure Administrator

Bootstrapping clean Debian 13 hosts on bare metal or cloud providers without cloud-init support.

- **Initialization Tool**: A standalone workstation bootstrap tool targets clean Debian 13 amd64 installations using the same baseline as Packer.
- **Template**: Copy and fill out [`ci/cloud-init/user-data.template.yaml`](./ci/cloud-init/user-data.template.yaml) with target identity, operator keys, and node repository.
- **Run Enrollment**:
  ```sh
  python3 ci/scripts/init-node.py root@server --user-data /private/path/location.user-data.yaml
  ```
- **Prerequisites**: Run from this commons checkout with Python 3.10+ and OpenSSH. The target must be Debian 13 amd64, reachable using a trusted SSH host key as root or an administrator with passwordless `sudo`. The node checkout must declare `APEX_ENROLLMENT_INTERFACE=1` and have a matching enrollment-aware `init.sh`. Use the supported template subset, not arbitrary cloud-config directives. Keep filled user-data and repository credentials outside Git; supply trusted repository host keys for SSH clones.
- **Bootstrap Dependencies**: The target seed installs distro `python3-yaml` for user-data validation; this is separate from the standard-library APEX core. ZFS setup needs matching running-kernel headers/modules. A mismatch stops with a diagnosis; enrollment does not reboot automatically.
- **Security Handover**: Safely switches SSH to key-only authentication on port 2222 (`adam@server`) protected by an automated rollback watchdog, activates UFW firewall rules, Docker iptables integration, and CrowdSec.
- **Resuming**: Follow the emitted standalone resume command. After committed access handover, use `python3 ci/scripts/init-node.py adam@server --port 2222 --user-data /private/path/location.user-data.yaml`; before handover, use the bootstrap endpoint after pending rollback. An uncertain handover requires checking which endpoint is active. Preserve any `--identity-file` option. Core `apex` has no remote `init` action.

### Developer & Contributor

Adding shared actions or improving the engine.

- **Conventions**: Maintain strict Python standard library compliance. No external runtime packages.
- **Public documentation**: Describe supported interfaces using synthetic examples. Keep deployment inventories, internal plans, operational runbooks, and host-specific configuration in their owning private repositories.
- **Setup**:
  ```sh
  python3 -m venv .venv
  . .venv/bin/activate
  python3 -m pip install ruff==0.17.2 'PyYAML>=6.0,<7'
  ```
- **Verification**: `make check` runs lint and both the core and standalone provisioning suites. `make test` (or `bash tests/run.sh`) runs both suites; discovery limited to `tests/` omits provisioning. PyYAML is needed for the provisioning tests, not core execution. Environment integration tests use `docker compose` or standalone `docker-compose` with `config`; no daemon is needed.
- **Linting**: Ruff checks correctness and security rules. Existing import-order, line-length, compact-statement, and annotation-style debt is explicitly deferred in `pyproject.toml` to avoid unrelated formatting changes. Without Ruff, `make lint` recursively checks Python compilation and fails on syntax errors; that fallback is not equivalent to Ruff. `make fmt` formats files and is a separate, deliberate operation.
- **Packer**: Run `packer init ci/packer/apex-debian-13.pkr.hcl` and `packer validate ci/packer/apex-debian-13.pkr.hcl` for template validation. A VM build or live enrollment is a separate operation; unit tests and template validation do not demonstrate a deployed host.

---

## Communication & Support

- **Issues**: <https://github.com/deytenit/apex.ermnvldmr.com/issues>
- **Email**: <personal@ermnvldmr.com>

## Copyright & License

Licensed under the [BSD-2-Clause License](./LICENSE).

---

**AI Assistance**: Portions of this project's source code and configuration may have been developed with the assistance of AI code-generation tools.
