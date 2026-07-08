# apex

The **commons** of an apex fleet: a stock-python engine (`apex <group>/<action>`) and
the shared core docker-compose, consumed by every node as the `commons/` git submodule.
The framework assumes no particular domain — every node declares its own identity in
`node.env`.

## Layout
- `apex` — launcher (python3, stdlib only); node `init.sh` symlinks it onto `PATH`.
- `engine/` — the runner: CLI, identity, overlay, context, and the `ctx.*` helper library.
- `actions/` — grouped actions; the path IS the name (`configure/ufw`, `tiers/link`, …).
- `compositions/apex/docker-compose.yml` — the shared, `${env}`-parameterized core compose.
- `vendor/` — pinned pure-python third-party (empty; see `vendor/VENDORED.md`).
- `tests/` — stdlib `unittest` suite (`tests/run.sh`).

Node-specific tooling lives in each node repo's `proprietaries/` — commons holds only
what every node shares.

## Usage (on a node)
`apex` acts on the **local checkout** — no `<node>` argument. Identity comes from
`node.env` (`APEX_NODE_HOST`, `APEX_NODE_FQDN`, `APEX_SUBNET`).
- `apex` / `apex --help` — list actions
- `apex configure/ufw` — render + apply this node's ufw config
- `apex configure` — run every `configure/*` in order
- `apex compose up` — bring up the node's compositions

## Constraints
git + python3 stdlib only. No pip/Node/Go/CUE. Composes are hand-authored (no codegen).

## License
MIT (see `LICENSE`).
