# Port parity map (bash -> python)

Behavioral parity is validated on a scratch host in Phase 3. This maps each ported
action to its bash source and the behaviors that must match.

| apex action | bash source (root commons) | must-match behaviors |
|---|---|---|
| configure/base | actions/configure/base | apt update+install curl/wget/git/rsyslog; rsyslog running; ufw + ufw-docker at ~/.local/bin |
| configure/ufw | actions/configure/ufw + root_template_ufw | host.rules via plain ufw; before/after.rules block inject (APEX markers, self-heal ROOT); docker rules via ufw-docker |
| configure/crowdsec | actions/configure/crowdsec | install if cscli absent; render configs->/etc/crowdsec; collections linux+traefik; firewall bouncer if bouncer file present |
| configure/cron | actions/configure/cron + root_template_crontab | render crontab; backup+install; rollback on empty/failure |
| configure/systemd | actions/configure/systemd | render each unit -> /etc/systemd/system; daemon-reload; enable+start .service |
| configure/routing | actions/configure/routing | ip_forward=1; fwmark 0x1->table100 rule; local default route table100 (idempotent) |
| configure | (new umbrella) | run base,ufw,crowdsec,cron,systemd,routing in order |
| sync/repository | actions/sync/repository | sync/<node> branch; add -A + commons submodule; commit; rebase origin/main (was trunk in the monorepo); force-with-lease; telegram |
| sync/packages | actions/sync/packages | apt --just-print upgrade Inst list; docker+skopeo digest compare; telegram info |
| backup/run | actions/sync/tiers (restic-backup) | init-if-needed; label-driven backup via resticontainer (discovers `restic.*`-labelled services, runs pre-hooks/stops, snapshots the resolved host paths) --host $APEX_NODE_HOST --tag biweekly --compression max; guard on a fresh snapshot; forget --prune 7/4/12; telegram |
| tiers/link | actions/tiers/link | node + per-project @tierN symlinks; shared/ links; .env symlink |
| tiers/useradd | actions/tiers/useradd | noroot-<proj> system users; noroot-shared group; APEX_UID/GID block in .env |
| tiers/chown | actions/tiers/chown | {tier}/{proj}=uid:gid; {tier}/shared/{proj}=uid:sgid+sticky; base=root:root |
| utils/extract-traefik-certs | actions/utils/extract-traefik-certs | traefik-certs-dumper v3; 600 perms; chown caller |
| utils/generate-happ-subscriptions | actions/utils/generate-happ-subscriptions | MOVED to daedalus proprietaries (node-specific tooling; commons keeps only shared stuff) |
| utils/lint-docker-compose | actions/utils/lint-docker-compose | dclint --fix via docker; --hook staged-only + restage; + anchor-IP check |
| compose | actions/compose | apex core first then services alpha; reverse on down; --dry-run; extra passthrough |

## Deliberate deltas (rename + fixes)
- root->apex everywhere (CLI, container names apex-*, telegram instance = APEX_NODE_HOST, ufw markers APEX.*, networks direct/enclave/socket).
- No <node> arg; identity from FQDN + node.env.
- backup superseded by the resticontainer migration (see below): the old whole-tier-root
  restic snapshot is replaced by per-service, `restic.*`-label-driven backups.
- ufw markers self-heal (delete ROOT + APEX blocks before inject).
- envsubst -> string.Template.safe_substitute; wget/curl download -> urllib. Unset template
  vars stay LITERAL (envsubst emptied them) — typos surface instead of silently blanking.
- crowdsec install auto-confirms (`apt-get install -y`; bash ran promptful `apt install`,
  which blocked on a tty and aborted non-interactive runs).
- pre-commit staged-compose pathspec widened to `*docker-compose.yml` — the old
  `*.docker-compose.yml`/root-anchored patterns never matched nested compose files.
- anchor-IP validation in utils/lint-docker-compose is a planned addition (SP3 §6.2).
- new opt-in `--dry-run` flags on configure/* have no bash counterpart; the default
  (flag absent) path is the ported behavior.
- ufw/crowdsec deploys keep a timestamped `.bak` next to overwritten system files
  (bash: none / `/tmp`) as a rollback net; tiers/useradd `.env` rewrites deliberately do
  NOT (no secret-bearing copies).
- non-conventional tool exit codes normalize to 1 (bash sometimes propagated e.g.
  apt-get's 100); 64/65/66 conventions preserved.

## Core-compose consolidation deltas (beyond the rename)
- icarus: traefik no longer publishes 53/tcp+udp (AdGuard DNS) from the shared file —
  restored via an icarus-only override layered in the node repo's include (Phase 2).
- icarus: ACME propagation-tuning flags and DEBUG log level drop to the shared defaults;
  traefik now runs noroot (APEX_UID) with file provider + HTTP/3 like daedalus.
- xray is profile-gated (`xray` in COMPOSE_PROFILES): daedalus + icarus opt in,
  morpheus does not (it never ran xray — owner decision 2026-07-05).
- loki (profile `loki`, no node opts in yet) is exposed via traefik at
  `https://<node-fqdn>/loki` behind basicauth (`APEX_LOKI_BASIC_AUTH` in the node .env).
- morpheus: dashboard auth changes basicauth (TRAEFIK_INTERNAL_API_AUTH) -> enclave IP
  allowlist; the adguard router labels exist without a backend (502, was 404, on /adguard).
- restic: per-node selective tier2 mounts replaced by whole-tier-root RO mounts
  (superset; snapshot paths preserved); RESTIC_COMPRESSION per-node via
  `APEX_RESTIC_COMPRESSION` (default auto).

## resticontainer migration (SP4 realized)
- The `restic` core service is replaced by `resticontainer`
  (`ghcr.io/deytenit/resticontainer`), a restic wrapper that reads per-service
  `restic.*` compose labels: `restic.enable`, `restic.backup.paths` (comma-separated
  container mount destinations — matched exactly, whole mount dir backed up),
  `restic.backup.stop`, and `restic.hooks.pre-backup`/`post-backup` (run via docker exec).
- It mounts the docker socket (list/inspect/exec/stop/start) and `/srv:/srv:ro`
  (`RESTIC_HOSTFS=/`); apex data + checkouts live under /srv and the `@tierN` mounts
  are absolute symlinks into /srv, so mounting /srv at the same path lets restic
  follow them and record the real host paths, and snapshots the union in one run.
- The whole-tier-root RO mounts and the explicit `/data/@tier1,2` backup args are gone;
  a service is backed up only if it carries `restic.enable=true`. DB consistency is
  per-service: a pre-backup hook dumps into a dedicated backed-up mount (e.g. postgres
  `pg_dump … > /dumps/dump.sql`), never the live data dir. `backup/run` now guards on a
  fresh snapshot landing for the host (a no-label repo would otherwise be a silent no-op).
