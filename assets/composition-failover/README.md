# Readiness-gated origin routing on Workers Free

This local ES-module Worker asset selects between two origins for one public HTTPS hostname. It implements the ingress part of the [composition replication RFC](../../docs/rfcs/composition-replication.md). It is not deployed and does not establish production high availability. Activate it only after backend HA and authenticated application readiness are verified.

The module has no runtime dependencies, paid bindings, KV, R2, storage or scheduled jobs. This directory does not enable billing, enroll an account, upload a Worker or change DNS/routes.

## Bindings

Set these string bindings through the operator's chosen Cloudflare deployment process. Keep the readiness token in a Worker secret, never a committed variable, URL or log.

| Binding | Value |
| --- | --- |
| `APEX_PUBLIC_HOST` | Existing public SSO hostname, for example `sso.example.com` |
| `APEX_ZONE` | The actual Cloudflare DNS zone, for example `example.com` |
| `APEX_ORIGIN_PRIMARY` | Dedicated DNS-only primary origin hostname in that zone |
| `APEX_ORIGIN_SECONDARY` | Dedicated DNS-only secondary origin hostname in that zone |
| `APEX_READY_PATH` | Dedicated absolute readiness path, for example `/.well-known/apex-ready` |
| `APEX_READY_TOKEN` | High-entropy secret shared only by this Worker and both readiness adapters |

Hostnames accept ordinary ASCII DNS labels, without a scheme, port, trailing dot, wildcard or IP literal. Case is normalized. Public and origin hostnames must be distinct and within the declared zone. Readiness paths accept ASCII letters, digits, `.`, `_`, `~`, `-` and `/`; queries, fragments, escapes, repeated slashes and dot traversal segments are rejected. The secret must be nonempty visible ASCII without whitespace. Invalid/missing bindings produce a generic no-store 503 before any fetch. Syntax validation cannot establish actual DNS ownership or prevent an operator from naming a separately delegated zone; deployment must verify both origins are in the actual same Cloudflare zone.

## Routing and readiness contract

Create two dedicated DNS-only origin records pointing to the participating servers; do not point either at the public hostname or a Worker. Retain the existing proxied public SSO DNS record. Use a **Worker Route** such as `sso.example.com/*` on that existing hostname, not a Worker Custom Domain. Both origins must serve HTTPS with a valid certificate and virtual host for the public name. Configure strict origin TLS verification. Disable any Workers Cache in front of this Worker and any cache policy that would bypass its invocation. [Cloudflare Routes](https://developers.cloudflare.com/workers/configuration/routing/routes/)

Require `global_fetch_private_origin` in the Worker compatibility flags and prohibit `global_fetch_strictly_public`. The strictly-public flag sends same-zone global fetches through Cloudflare's public routing layer and can re-enter this Worker; a readiness probe would then hit its public-path rejection instead of the origin. Private-origin behavior routes same-zone subrequests to the origin, bypassing mapped Workers and Cloudflare security settings, so the origin adapter must still enforce its own authentication. [Global fetch compatibility](https://developers.cloudflare.com/workers/configuration/compatibility-flags/#global-fetch-strictly-public)

Set `compatibility_date` to **2024-11-11 or later** and do not set `cache_option_disabled`. `cache_option_enabled` became the default on that date; the example also enables it explicitly. Without this behavior, constructing requests with `cache: "no-store"` throws instead of dispatching. [Request cache compatibility](https://developers.cloudflare.com/workers/configuration/compatibility-flags/#enable-cache-no-store-http-standard-api)

The required non-secret upload metadata fields are:

```json
{
  "main_module": "worker.mjs",
  "compatibility_date": "2026-10-10",
  "compatibility_flags": [
    "global_fetch_private_origin",
    "cache_option_enabled"
  ]
}
```

Use this as the multipart API `metadata` portion, with the ES-module file part named `worker.mjs`. Bindings and the readiness secret are configured separately through the operator-owned enrollment process; they are deliberately absent from this example, as are paid bindings. This metadata is not a complete enrollment or route activation command. Verify the resulting effective settings before activation, including after later uploads. [Multipart upload metadata](https://developers.cloudflare.com/workers/configuration/multipart-upload-metadata/)

Every accepted HTTPS request first probes the primary with a GET at `https://APEX_PUBLIC_HOST/APEX_READY_PATH`. The request carries only `X-APEX-Ready-Token`, rather than client cookies or authorization. `cf.resolveOverride` selects the DNS-only origin while the URL remains the public hostname. Only status 204 is ready; redirects are manual and never accepted. Each probe has a two-second deadline. Unread response bodies are canceled, including responses arriving after timeout. Failed primary readiness leads to a secondary probe; neither ready produces a no-store 503. There is no readiness cache or background health state.

The dedicated origin adapter must authenticate the token, reject its absence/incorrect value and prove that this application instance can safely serve traffic: required dependencies, compatible configuration, current write authority/quorum, session state and any application-specific prerequisites. Authelia `/api/health`, an nginx 204, a TCP check or a running container alone is insufficient. Protect adapter aliases and normalize paths consistently at the origin. The Worker rejects public requests to the configured readiness path (also with a query or percent-encoded spelling), and removes every client-supplied readiness header before application dispatch. This asset does not implement or enroll the backend adapter.

Cloudflare documents `resolveOverride` as DNS substitution while retaining the URL's Host, subject to both names belonging to the same zone. The asset retains the HTTPS public URL for routing; actual origin TLS SNI/certificate selection and routing must be demonstrated natively before activation. No local Node test proves Cloudflare DNS/TLS behavior. [Request API](https://developers.cloudflare.com/workers/runtime-apis/request/)

## Application request behavior

After selection, the original method, URL/path/query, body, cookies and authorization are forwarded once. The body is streamed; no request-body buffering or replay copy is made. The client readiness header is removed. Application redirects remain manual, and response status, body, Location and separate Set-Cookie headers are preserved.

**No application request is replayed after an origin failure, including GET callbacks.** A fetch exception returns a generic no-store 502; an origin HTTP error is returned with its original status/body. There is no second probe or alternative-origin dispatch after this point. Readiness can become stale between probe and dispatch; failover applies to subsequent requests. Application requests have no additional Worker timeout so streaming is not cut off; platform/client deadlines still apply.

All probe and application fetches use `cache: "no-store"`, `cacheEverything: false` and negative `cacheTtlByStatus`. Ordinary responses override browser/CDN cache headers to no-store. There is no Cache API use. Verify native cache bypass with cookies, authorization, redirects and authentication error responses before enrollment. [Fetch caching](https://developers.cloudflare.com/workers/runtime-apis/fetch/)

A platform status-101 response is returned unchanged to preserve Cloudflare's WebSocket upgrade object. The Worker does not terminate or inspect WebSocket messages, migrate existing connections, reconnect clients or claim protocol HA. Ordinary response bodies remain streams. Native WebSocket behavior is an optional deployment test if the application needs it; the local test only checks unchanged forwarding of the platform response. XHTTP and XHTTP-over-Cloudflare are excluded from this SSO HA scope. [Workers Response API](https://developers.cloudflare.com/workers/runtime-apis/response/)

Other hostnames, non-default ports and non-HTTPS requests receive no-store 421. The public route must already have the intended HTTPS policy. Client errors and unavailable responses contain no secret diagnostics; the module emits no logs.

## Free-tier deployment gates

Workers Free currently provides **100,000 incoming requests per day per account**, reset at midnight UTC, and **10 ms CPU per HTTP request**. Quota is shared with the account's other Workers. This asset performs two fetch subrequests normally (one probe plus application dispatch), or three when primary readiness fails. Waiting for network I/O does not count toward CPU time, but actual Worker CPU consumption must be measured; Node wall time is not that measurement. [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)

Keep the account on Workers Free. Do not enable a paid plan, paid load balancer or paid bindings for this asset. An operator must inspect existing account usage and peak traffic to confirm the free quota is sufficient. This is a no-added-cost deployment candidate, not unlimited availability.

Set the Worker Route to **fail closed**. On Free quota exhaustion, Cloudflare documents a 1027 error page for this mode; fail open bypasses the Worker and is unacceptable here. The module cannot enforce the platform route setting or prevent quota exhaustion. Record and verify that setting before activation.

Operator-owned enrollment gates:

1. Validate backend HA, adapter authentication/readiness, dependency loss and write-authority withdrawal independently of this Worker.
2. Confirm account Free plan/quota, same-zone DNS-only origin records, public proxied Route, strict TLS, native SNI/Host at each origin, required private-origin/cache compatibility flags and date, and no Worker/route recursion or pre-Worker caching.
3. Test native primary/secondary readiness, both-unready failure, redirects, cookies, cache bypass, POST and GET callback non-replay, streaming and any required WebSockets. Measure Worker CPU and account request headroom.
4. Verify fail-closed quota handling and exercise location loss, network partitions, stale/rejoining origins and recovery. Record observed recovery time and session behavior before making an HA claim.
5. Activate only after those gates pass with the intended backend HA. Maintain a documented operator rollback that does not silently route to an unsafe origin.

## Local verification

From the shared APEX repository root, with Node available:

```sh
node --test assets/composition-failover/worker.test.mjs
```

Tested with Node v24.11.1 using its built-in test runner and Web APIs. Tests call the exported handler and replace only the external fetch boundary, with fake timers for probe deadlines. They do not call Cloudflare or use credentials. No npm install, new dependency or change to the default Python test workflow is required. Native Cloudflare deployment is deliberately outside this local test's proof.
