# Container Registry Tag Queries

This reference details how to discover available image tags across common container registries without requiring external tooling.

## 1. Docker Hub (`hub.docker.com`)

Docker Hub provides an unauthenticated v2 API for public repositories.

### Official Images (Library)
For top-level images like `postgres`, `redis`, `traefik`, `mariadb`:

```bash
IMAGE="postgres"
curl -s "https://registry.hub.docker.com/v2/repositories/library/${IMAGE}/tags?page_size=100" \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
tags = [t["name"] for t in data.get("results", [])]
print("\n".join(tags[:30]))
'
```

### Community / User Images
For namespace images like `adguard/adguardhome`, `prom/prometheus`, `grafana/alloy`:

```bash
ORG="adguard"
IMAGE="adguardhome"
curl -s "https://registry.hub.docker.com/v2/repositories/${ORG}/${IMAGE}/tags?page_size=100" \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
tags = [t["name"] for t in data.get("results", [])]
print("\n".join(tags[:30]))
'
```

---

## 2. GitHub Container Registry (`ghcr.io`)

GHCR implements OCI distribution spec. Public image tags can be queried with an anonymous bearer token.

```bash
IMAGE="xtls/xray-core"
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:${IMAGE}:pull" | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer ${TOKEN}" "https://ghcr.io/v2/${IMAGE}/tags/list" \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
tags = data.get("tags", [])
print("\n".join(sorted(tags, reverse=True)[:30]))
'
```

---

## 3. Quay.io (`quay.io`)

Quay provides a REST API:

```bash
REPO="repository_name"
curl -s "https://quay.io/api/v1/repository/${REPO}/tag/?limit=50&onlyActiveTags=true" \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
tags = [t["name"] for t in data.get("tags", [])]
print("\n".join(tags[:30]))
'
```

---

## 4. SemVer Matching & Sorting

When filtering tags to find the latest stable version matching the current container's flavor:

```python
import re

def parse_semver(tag: str):
    # Match patterns like v1.2.3, 1.2.3-alpine, 16.4
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(.+))?$", tag)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    flavor = m.group(4) or ""
    return (major, minor, patch, flavor)
```

Rules for candidate selection:
1. Preserve flavor: If currently running `16-alpine`, match only tags with suffix `-alpine`.
2. Ignore pre-releases: Filter out tags containing `alpha`, `beta`, `rc`, `dev`, `test`, or git SHAs.
3. Classify:
   - Same `major`, higher `minor` or `patch` -> **Minor / Patch**.
   - Higher `major` -> **Major** (requires Step 5 approval).
