# RFC Reusable composition replication patterns

Status: Accepted patterns; SSO revised to two locations with controlled promotion. The rollout remains pending migration, routing, and failure verification; this document does not claim achieved high availability.

## Purpose and decision

APEX should make it repeatable to deploy another instance of an individual composition on another node and verify its declared failure behavior. Nodes and repositories always remain distinct. Applications are mostly unrelated, and duplicating configuration or running dedicated supporting services is acceptable when it preserves understandable ownership.

The unit of replication is a logical application deployment, represented by separately owned Compose projects on participating nodes. Replication does not mean cloning a node, synchronizing repositories, sharing every composition, or applying one storage mechanism to every application.

Adopt a small catalog of replication patterns, a common deployment contract, and a common verification workflow. Use application and database native mechanisms for data replication and leader election. Shared backend clusters are optional. SSO is the first worked example, not the architecture imposed on later applications.

## Existing foundations

APEX is a Python standard-library engine with node-local actions and handwritten Compose files. Each node repository pins commons and owns its compositions, configuration, and proprietary actions. Keep these characteristics.

`node.env` declares node identity and a node-local `APEX_SUBNET`. The core composition creates a Docker bridge on that subnet. The checked-in fleet convention assigns separate /24 networks to nodes within a larger cluster address block. Existing firewall and routing configuration includes Xray transparent-proxy rules; this is not evidence that arbitrary replication protocols already work across every node.

`apex compose` currently orchestrates local projects, with the core first. The action overlay provides a natural extension point for application-specific operations. Tier storage separates runtime state and secrets from tracked Compose definitions.

## Ownership and terminology

A **node** is an independently managed server with its own repository, identity, addresses, storage, credentials, and lifecycle.

A **composition instance** is a local Compose project. Its repository owns its complete definition, even when another repository owns a similar project.

A **replication group** identifies composition instances providing the same logical application. One node can participate in several unrelated groups, each with different members, roles, backend choices, and availability guarantees. A Compose project name alone does not establish group membership.

A **replication domain** identifies the permitted network scope for a group. The user referred to this scope as Na. This RFC treats it as a set of mutually reachable participating nodes rather than requiring a shared layer-2 network. The label is independent of the engine's existing node-local `APEX_SUBNET`; no address allocations or cross-cluster membership are implied.

A **failure domain** identifies what can disappear together, such as a physical site. Distinct node names or subnet labels are not sufficient evidence of independent failures.

A **backend group** provides a data service to one or more application groups. It has its own members and failure contract. It can be dedicated to one application or deliberately shared by compatible applications.

## Supported patterns

| Pattern | Suitable application | Replication and failover responsibility |
| --- | --- | --- |
| Stateless copies | Instances can serve concurrently without unique mutable local state | Deploy equivalent configuration; route to ready instances |
| Copies using replicated backends | Application supports concurrent instances and external data services | Application instances share logical state through compatible replicated backends; backends own consistency and failover |
| Application-native cluster | Application provides supported membership, replication, and recovery | Follow its native topology and lifecycle; expose readiness and recovery through APEX conventions |
| Single active instance with standby | Application requires exclusive ownership of mutable state | Use a supported consistent state-transfer method and proven fencing before automatic promotion |
| Recovery-only deployment | Application has no validated safe live-replication arrangement | Reproducible restore and explicit activation; report that automatic availability is unsupported |

Every service can be assessed through this catalog. Not every service can safely provide concurrent active copies. Supporting an arbitrary service means allowing an appropriate adapter or documented recovery pattern, not promising automatic active-active operation for unmodified software.

A composition can combine patterns: stateless HTTP instances, a replicated SQL backend, and a singleton scheduled worker. Starting duplicate workers or schedulers is a separate correctness decision from starting duplicate web containers.

Do not synchronize live database directories as a generic replication mechanism. Applications using SQLite require an application-supported solution or exclusive-writer failover; placing their database on a network filesystem is not a universal fix. [SQLite network storage guidance](https://www.sqlite.org/useovernet.html)

## Required deployment contract

Each participating repository must record the following non-secret information alongside the composition using the project's established configuration and documentation conventions. This RFC does not introduce a new manifest parser or require generated Compose files.

| Contract element | Required content |
| --- | --- |
| Identity | Logical group name, local instance identity, replication domain, failure domain, topology revision |
| Members | Explicit peer identities, stable reachable endpoints, and desired membership roles |
| Pattern | Selected replication pattern and supported application/backend versions |
| State inventory | Every persistent volume, uploaded file store, session store, user directory, queue, and mutable configuration source |
| State ownership | Replication method, write authority, consistency limits, and recovery procedure for each state class |
| Dependencies | Named backend groups, access requirements, and the failure assumptions of each dependency |
| Configuration | Required common settings, intentionally local settings, secret references, and update procedure |
| Traffic | Public and internal client endpoints, readiness conditions, routing and withdrawal behavior |
| Lifecycle | Initial creation, joining, draining, upgrade order, departure, rejoin, and full-outage recovery |
| Guarantees | Maximum recovery time target, acknowledged-data-loss policy, session behavior, and supported failure scenarios |

Desired roles must not overwrite runtime decisions. For example, a replica promoted by Sentinel must not be forcibly returned to an obsolete role on every ordinary restart.

Repositories can adopt application changes independently only within the documented version-compatibility window. Changes to group membership, shared schemas, or wire protocols require coordinated sequencing. Planned membership changes must validate agreement among the participating reachable nodes against the native system's authoritative membership. Removing a failed member follows the native recovery procedure and must not require that failed member to acknowledge the change.

Keep private keys, credentials, password hashes, and application encryption secrets outside tracked configuration. State explicitly which application secrets must match across replicas; node identity and node-specific credentials remain distinct.

## APEX responsibilities

APEX should provide reusable pattern documentation, preflight checks, composition-scoped lifecycle entry points, and a consistent way to inspect readiness and replication health. A future implementation should use the existing action and overlay model. Exact new command names and machine-readable schemas belong to the implementation design after this RFC is accepted.

Actions operate on explicitly selected local compositions. Optional SSH invocation transports the same operations to named nodes; it does not create a fleet-wide desired-state controller. APEX must not make automatic quorum decisions or promote database primaries based on its own ping checks.

Checks must distinguish running containers from a service that can safely serve traffic. They must report incompatible versions, unreachable required peers, mismatched membership, missing configuration, unsafe state-sharing arrangements, and absent capacity for the declared failure target.

Shared helpers may cover common tasks. Native backend tools retain ownership of elections, replication, fencing, and recovery. Application-specific behavior stays in a bounded adapter or runbook rather than an expanding set of SSO conditionals in the engine.

## Networking and routing

Reuse an existing private transport when tests demonstrate that it supports the application's actual protocols. Validate advertised addresses, bidirectional connectivity, required TCP and UDP ports, reconnect behavior, and relevant MTU constraints. Do not silently replace the fleet's routing with a new overlay.

Validate the peer identities actually learned after reconnect, not just the configured addresses or successful initial connections. Some protocols derive membership from the incoming TCP source address: Galera combines that address with the advertised port. A TCP proxy that replaces multiple peer sources with a gateway address does not preserve this contract. Such groups need a transport that preserves distinct peer source addresses. [Galera handshake implementation](https://github.com/codership/galera/blob/release_26.4.27/gcomm/src/gmcast_proto.cpp#L222)

The current fleet rollout uses the existing Xray TPROXY tunnels only. WireGuard is excluded. Restrict peer addresses and ports, reject fallback through unrelated routes when a peer is unreachable, and verify reconnect and container-network replacement behavior. A protocol incompatible with this transport requires a different application replication pattern, not an unapproved network overlay.

Preserve distinct node-local bridge networks and addresses. Docker service names and host-local container IP assumptions must not be mistaken for cross-host service discovery. Peer endpoints must be explicitly reachable from the consumers that use them.

A logical application endpoint routes only to ready instances. Dependency failure and loss of write authority must affect readiness. Both browser-facing endpoints and internal application calls must survive the same location-loss scenario. A single reverse proxy on one participating site must not be the only traffic entry point.

Plain round-robin DNS distributes addresses but does not supply application health checks. DNS-based failover also needs its caching behavior included in the measured recovery time. [DNS load balancing](https://www.cloudflare.com/learning/performance/what-is-dns-load-balancing/)

Automatic failover must keep working when the operator laptop and APEX CLI are absent. Its runtime mechanisms belong to the deployed services and traffic infrastructure.

## Shared and dedicated backends

Allow both arrangements. Prefer dedicated resources when versions, retention, durability, eviction policy, performance, or trust boundaries differ. Reuse an existing compatible cluster when doing so materially simplifies operations and its shared failure impact is accepted.

Several applications can use separate databases and credentials within one MariaDB cluster; each does not automatically require a new Galera installation. Applications requiring PostgreSQL need an appropriate PostgreSQL solution instead. Shared database credentials are not an acceptable shortcut.

Caches, sessions, and durable queues must not be combined solely because all speak the Redis protocol. Separate Valkey replication groups remain valid, and Sentinel can monitor multiple named groups. A Sentinel voting set is not a universal witness for unrelated database technologies. [Valkey Sentinel](https://valkey.io/topics/sentinel/)

Application placement and backend placement must satisfy the whole dependency graph. Two application copies are not location-independent when both require an unreplicated database on one of those locations.

## Lifecycle and failure behavior

Initial creation and joining are different operations. Joining must verify the group identity and synchronize from the authorized source. Existing non-empty state is not overwritten implicitly. Formatting storage, discarding divergent state, and forced cluster recovery are explicit operations.

Ordinary deployment must not bootstrap a new database cluster when peers are unreachable. A returning member catches up and passes readiness checks before it receives traffic. Repeated operations should converge or stop with an actionable diagnosis rather than silently forming a second cluster.

Upgrade one failure domain at a time where the native system supports it. Verify redundancy and capacity before removing a member. Schema migration ownership and compatible mixed-version operation must be documented per application; reverting a container image does not automatically reverse a database migration.

Automatic promotion of an exclusive writer requires a native election mechanism or fencing that prevents the previous writer from continuing. If this prerequisite is absent, classify promotion as manual rather than claiming safe automatic failover.

Backups remain independent of replication and cover accidental deletion, corruption, and total group loss. Restoring a backup must not introduce an additional active writer into an existing group without its join procedure.

## SSO first application

Daedalus and Metis are separate repositories and application locations. The selected SSO pattern is one active deployment and one standby, with controlled manual promotion. Icarus is not a member of the target SSO topology. Other compositions on these nodes do not become replicas by association.

The target uses native MariaDB primary/replica replication and a primary/replica session store over the existing Xray tunnels. It excludes Galera, its arbitrator, Sentinel elections, and custom automatic promotion controllers. The standby must not accept application writes until an operator has fenced the previous writer and explicitly promoted the replacement. An unreachable SSH or tunnel endpoint alone does not prove fencing. Native replication status and the last applied position must be checked and any possible data loss recorded before promotion.

The existing public users source is a manually maintained YAML file. The selected replacement is LLDAP backed by MariaDB, with its enrolled directory preserved during the change from the staged Galera deployment to ordinary replication. Existing account identifiers, profile attributes and application groups must survive migration. The operator has already completed the one-time password enrollment; changing replication must not require another enrollment or overwrite the directory. Keep the old public SSO authoritative until migration checks pass. [Authelia LLDAP integration](https://www.authelia.com/integration/ldap/lldap/)

The two identity locations use `198.18.16.150` on Daedalus and `198.18.18.150` on Metis. The existing SSO frontend addresses remain separate. These are explicit allocations for this deployment, not reserved addresses or mandatory topology for other APEX applications. The staged Icarus voting services must be retired only after verifying that the preserved data and new replication no longer depend on them; unrelated Icarus applications remain outside this change.

Stage the new backend beside the existing public SSO database. Preserve a verified backup and use a consistent logical export for migration. Ordinary container restart must never initialize a replacement database, replay a seed snapshot, or promote a replica. Test direct Daedalus–Metis Xray connectivity without Icarus as an intermediary. Returning former primaries remain fenced until explicitly resynchronized as replicas.

Asynchronous database and session replication can lose acknowledged recent changes. This pattern does not promise seamless preservation of every account change, session, or revocation. The promotion procedure must document the replication position, possible loss and whether users need to log in again. Session invalidation after promotion must be tested so that stale replicated sessions do not silently restore revoked access.

The earlier automatic recovery target of 60 seconds is superseded by controlled promotion. Loss of the active stateful deployment can interrupt authentication until promotion completes; loss of the standby must not stop the active deployment. Record the measured promotion and recovery time rather than claiming an automatic recovery bound.

Cloudflare traffic failover is requested for `sso.ermnvldmr.com`, `jef.ermnvldmr.com`, `jpl.ermnvldmr.com`, `sea.ermnvldmr.com`, and `imh.ermnvldmr.com`, using the no-additional-cost Worker approach. Traffic selection must not promote databases or treat an unpromoted standby as ready. Each hostname needs independently verified origin routing and application readiness. Two ingress paths to a single application backend provide ingress redundancy only; they do not replicate that backend or its data. Preserve existing streaming, WebSocket, authentication, and path-specific routes.

## Adding the next application

1. Select the compositions and target nodes; keep node identities and unrelated projects untouched.
2. Inventory mutable state, background jobs, dependencies, and application-supported concurrency.
3. Select a pattern. Reuse compatible infrastructure or provision dedicated supporting groups deliberately.
4. Add the local composition and deployment contract to each participating repository. Provide configuration, secrets, and reachable endpoints.
5. Run preflight checks, seed or join through the native mechanism, and confirm synchronization.
6. Add the ready instance to traffic routing.
7. Execute the failure suite and record the measured guarantee before describing the application as highly available.

A stateless dashboard should follow these steps without database machinery. A SQL-backed application may reuse a cluster. A SQLite-backed application may need a standby arrangement or remain recovery-only. This range is intentional for a fleet of unrelated applications.

## Acceptance and rollout

Validate the framework against at least a stateless composition and the SSO stateful example before generalizing helpers further. Do not migrate unrelated existing applications as part of the first implementation.

For each claimed automatic-availability profile, test loss of every location individually, process failure, network partition, stale/rejoining members, and a rolling update. Observe end-user operations and dependency health, not merely container exit codes. Test without the operator laptop participating.

Record recovery duration, acknowledged data lost, session behavior, whether conflicting writers existed, and whether manual intervention was needed. Full-group loss and backup restoration have a separate recovery test and do not inherit the single-location availability promise.

A failed prerequisite must produce an honest unsupported or degraded result. The word replicated alone must not imply the availability target has been verified.

## Relationship to remote initialization

Remote initialization remains a separate companion design: a local `apex init` would prepare a Debian server using the same versioned baseline and supported enrollment inputs as the golden-image/cloud-init path. It ends before application state migration or replication-group creation. Replication recipes must not format disks or initialize clusters as a side effect of preparing a host.

## Alternatives considered

Whole-node/repository mirroring violates the ownership model and couples unrelated applications. A mandatory shared database platform couples applications that may need different engines or policies. A universal volume-sync flag cannot supply arbitrary application consistency. A fleet scheduler is a possible future infrastructure choice, but introducing one is unnecessary for this RFC and would not itself solve application data replication.

The proposed pattern catalog preserves independent repositories, permits useful redundancy, and makes later composition replicas follow a repeatable, testable process.
