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
| sync/repository | actions/sync/repository | sync/<node> branch; add -A + commons submodule; commit; rebase origin/trunk; force-with-lease; telegram |
| sync/packages | actions/sync/packages | apt --just-print upgrade Inst list; docker+skopeo digest compare; telegram info |
| backup/run | actions/sync/tiers (restic-backup) | init-if-needed; backup /data/@tier1,2 --host --tag com-ermnvldmr-root-<node>; forget --prune 7/4/12; telegram |
| tiers/link | actions/tiers/link | node + per-project @tierN symlinks; shared/ links; .env symlink |
| tiers/useradd | actions/tiers/useradd | noroot-<proj> system users; noroot-shared group; APEX_UID/GID block in .env |
| tiers/chown | actions/tiers/chown | {tier}/{proj}=uid:gid; {tier}/shared/{proj}=uid:sgid+sticky; base=root:root |
| utils/extract-traefik-certs | actions/utils/extract-traefik-certs | traefik-certs-dumper v3; 600 perms; chown caller |
| utils/generate-happ-subscriptions | actions/utils/generate-happ-subscriptions | build image; run generator input->output |
| utils/lint-docker-compose | actions/utils/lint-docker-compose | dclint --fix via docker; --hook staged-only + restage; + anchor-IP check |
| compose | actions/compose | apex core first then services alpha; reverse on down; --dry-run; extra passthrough |

## Deliberate deltas (rename + fixes)
- root->apex everywhere (CLI, container names apex-*, telegram instance com.ermnvldmr.apex.*, ufw markers APEX.*, networks direct/enclave/socket).
- No <node> arg; identity from FQDN + node.env.
- restic core service gains read-only /data/@tier1,2 mounts (makes existing backup command functional).
- ufw markers self-heal (delete ROOT + APEX blocks before inject).
- envsubst -> string.Template.safe_substitute; wget/curl download -> urllib.
