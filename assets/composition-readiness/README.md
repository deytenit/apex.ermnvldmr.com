# Authenticated composition readiness adapter

This dependency-free Python adapter exposes one local HTTP endpoint for the readiness-gated Worker and origin proxy. By default it runs an operator-supplied executable for every authenticated probe; exit0 means ready, every other outcome means unavailable. The executable must establish the application's actual readiness contract. A successful TCP connection or container health flag is not enough for replicated SSO.

Example private configuration:

```json
{
  "host": "127.0.0.1",
  "port": 9092,
  "path": "/.well-known/apex-ready",
  "token_file": "/private/readiness-token",
  "command": ["/usr/local/bin/python3", "/assets/probe.py", "--config", "/private/probe.json"],
  "timeout": 1.5,
  "slots": 4,
  "single_flight": false
}
```

Run `python3 adapter.py --config /private/adapter.json` inside the application frontend namespace. Keep the token and probe credentials private. A64-character lowercase hexadecimal random token is suitable. This adapter does not need issuer private keys, a Docker socket, root, writable application data or network-administration capabilities. Bind only to loopback; route authorized origin readiness through a separately validated local proxy.

Only GET at the exact canonical path, with one matching `X-APEX-Ready-Token` header, can execute the fixed command. Missing/wrong/duplicate credentials return403; other paths404; supported non-GET methods405. Ready returns204; probe failure, timeout or concurrency exhaustion returns503. These responses have empty bodies and `Cache-Control: no-store`. The adapter does not log requests, tokens or command output, cache success, or accept a client-supplied command. Each connection closes after its response.

Probe timeout defaults to1.5seconds and can be configured up to3seconds. With a3-second probe, allow4seconds at the readiness proxy and5seconds at the Worker. A timeout kills the probe's process group. Probe slots and request threads are bounded; slow socket operations time out after two seconds. Operators must account for proxy overhead and actual probe execution time. Probe programs must not detach into a new session or launch background jobs.

Optional `single_flight: true` lets overlapping authenticated requests share one running fixed-command probe. It accepts only a JSON boolean and defaults to false. Waiters use that probe's remaining monotonic deadline; they never queue another probe for the same request. The existing connection limit (`slots * 2 + 4`) also bounds waiting requests. Timeout or shutdown returns unavailable and terminates the probe group. Completed results are not cached: each subsequent request starts a fresh probe, with no success TTL or failure cache. Coalescing reduces concurrent work but does not make a slow individual probe meet its deadline.

For the controlled active/standby SSO pattern, the probe must reject a node not explicitly configured as active, verify completed migration and unchanged Authelia configuration, and check local Authelia health, authenticated SQL access/schema with read_only disabled, directory access, and an authenticated session write/read/delete over verified TLS. All dependencies belong to the selected local identity instance. These checks do not establish exclusive primary ownership: the operator must fence the previous writer before promotion, and the standby application and ingress stay disabled. No quorum, Sentinel, or session-controller authority is required by this pattern. The generic adapter supplies none of those application checks; do not deploy a placeholder-success probe or claim these tests establish HA.

Run `python3 test_adapter.py` for loopback HTTP and subprocess behavior checks. No third-party packages are required. Production native image/probe/proxy integration and location-loss tests remain separate acceptance gates.
