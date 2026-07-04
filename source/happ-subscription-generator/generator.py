#!/usr/bin/env python3
"""
HAPP subscription generator.

Produces one .psub file per user. Each file contains:
  line 1 : happ://routing/onadd/<base64>   (routing profile, unchanged behaviour)
  line N : a share link for every config the user has

Design
------
The config is written 1:1 against the HAPP / Xray share-link format. A template
is just the literal pieces of a URI:

    scheme   ->  vless:// , hy2:// , trojan:// , ss:// , socks:// , vmess://
    host     ->  the @host part
    port     ->  the :port part
    id       ->  the userinfo before @  (uuid / password / auth)
    params   ->  the ?a=b&c=d query, keys are the REAL link keys
                 (type, security, sni, fp, alpn, flow, encryption,
                  pbk, sid, spx, mode, path, host, serviceName,
                  headerType, mux, obfs, ... whatever the link supports)

Nothing is translated or guessed. Whatever you put in `params` is what goes in
the link, url-encoded. This keeps the generator dumb and the config honest.

Per-user `configs` entries deep-merge onto the named template, so a user only
supplies the bits that differ (their `id`, their `params.sid`, etc.).

The `#remark` is built from template metadata (country / nodes) + the user name.
"""

import json
import base64
import sys
import os
import copy
from urllib.parse import urlencode, quote

# ISO 3166-1 alpha-2 -> (flag, display name). Extend as needed.
COUNTRY_MAP = {
    "LV": ("\U0001F1F1\U0001F1FB", "Latvia"),
    "RU": ("\U0001F1F7\U0001F1FA", "Russia"),
    "KZ": ("\U0001F1F0\U0001F1FF", "Kazakhstan"),
    "BY": ("\U0001F1E7\U0001F1FE", "Belarus"),
}


def deep_merge(base, override):
    """Recursively merge `override` into a copy of `base`. Lists/scalars replace."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def routing_link(routing):
    """happ://routing/onadd/<base64(json)>"""
    payload = json.dumps(routing, separators=(",", ":")).encode()
    return "happ://routing/onadd/" + base64.b64encode(payload).decode()


def build_remark(node, user_name):
    code = node.get("country", "")
    flag, name = COUNTRY_MAP.get(code, ("\U0001F310", code or "??"))
    transport = node.get("params", {}).get("type", "tcp")
    chain = " > ".join(node.get("nodes", ["unknown"]))
    return f"{flag} {name} [{transport}] | {chain} | {user_name}"


def build_link(node, user_name):
    """Assemble one share link from a fully-merged node dict."""
    scheme = node["scheme"]
    host = node["host"]
    port = node["port"]
    userinfo = node.get("id", "")

    # Drop empty params so the query stays clean; preserve insertion order.
    params = {k: v for k, v in node.get("params", {}).items()
              if v is not None and v != ""}

    # Lists (e.g. alpn) -> comma-joined; everything else stringified.
    flat = {}
    for k, v in params.items():
        flat[k] = ",".join(map(str, v)) if isinstance(v, list) else str(v)

    query = urlencode(flat, safe="/:")
    remark = quote(build_remark(node, user_name))

    base = f"{scheme}://{userinfo}@{host}:{port}"
    return f"{base}?{query}#{remark}" if query else f"{base}#{remark}"


def main():
    if len(sys.argv) < 3:
        print("Usage: generator.py <input_json> <output_dir>")
        sys.exit(1)

    input_path, output_dir = sys.argv[1], sys.argv[2]

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    routing = routing_link(data.get("routing", {}))
    templates = data.get("templates", {})
    users = data.get("users", [])

    os.makedirs(output_dir, exist_ok=True)

    for user in users:
        name = user["name"]
        out_path = os.path.join(output_dir, user.get("psub", name))

        lines = [routing]
        for entry in user.get("configs", []):
            tmpl = entry.get("template")
            node = deep_merge(templates.get(tmpl, {}), entry) if tmpl else entry
            lines.append(build_link(node, name))

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Generated: {out_path}")


if __name__ == "__main__":
    main()
