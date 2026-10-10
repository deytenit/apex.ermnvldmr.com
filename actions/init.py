"""Initialize a Debian target without resolving a workstation node identity."""
from engine.descriptor import Arg, Meta, Opt
from engine.provisioning.transport import initialize

METADATA = Meta(summary='Initialize Debian 13 through protected SSH enrollment.', args=[
    Arg('target', 'Bootstrap USER@HOST; root or an existing administrator with sudo -n'),
    Opt('--user-data', 'Supported APEX cloud-config file'),
    Opt('--port', 'Bootstrap SSH port; otherwise use SSH configuration'),
    Opt('--identity-file', 'SSH private identity file; otherwise use SSH configuration/agent'),
])


def run(ctx, args):
    initialize(ctx.paths.commons, args.target, args.user_data, args.port, args.identity_file,
               progress=ctx.log.info)
    return 0
