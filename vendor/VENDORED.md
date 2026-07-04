# Vendored third-party libraries

Runtime is **git + python3 stdlib only**. This directory is currently **empty**.

If a pure-python library ever becomes truly necessary:
1. It must be **pure python** (no C extensions — there is no build step).
2. Check it into `vendor/<pkg>/`. The `apex` launcher adds `vendor/` to `sys.path`.
3. Add one record row below: `name, version, source URL, git SHA, sha256`.
4. Never `pip install` at runtime. Updates are a manual re-vendor + record bump.

| name | version | source | git sha | sha256 |
|------|---------|--------|---------|--------|
| _(none)_ | | | | |
