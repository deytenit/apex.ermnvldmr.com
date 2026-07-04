# actions/configure/routing.py
"""Port of configure/routing — idempotent node routing (ip_forward, fwmark rule, table 100)."""
from engine.descriptor import Meta

METADATA = Meta(summary="Idempotent node-level routing rules and routes.")

def run(ctx, args):
    log, s = ctx.log, ctx.sys
    log.info("Applying persistent node-level routing configuration...")
    s.sudo(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    if s.ok(["bash", "-c", "sudo ip rule show | grep -q 'fwmark 0x1 lookup 100'"]):
        log.info("IP rule for mark 0x1 already exists.")
    else:
        s.run(["bash", "-c", "sudo ip rule add fwmark 0x1 lookup 100 priority 100 || true"])
        log.info("IP rule added for mark 0x1.")
    if s.ok(["bash", "-c", "sudo ip route show table 100 2>/dev/null | grep -q 'local default dev lo'"]):
        log.info("Local route in table 100 already exists.")
    else:
        s.run(["bash", "-c", "sudo ip route add local 0.0.0.0/0 dev lo table 100 || true"])
        log.info("Local route added to table 100.")
    log.success("Routing configuration applied.")
