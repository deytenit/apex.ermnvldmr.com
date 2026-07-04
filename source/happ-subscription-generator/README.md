# HAPP Subscription Generator

**Disclaimer:** This tool is intended exclusively for **educational purposes** and **private networking** research. It is designed to demonstrate how to programmatically generate and manage subscription configurations for distributed network components.

This tool generates `.psub` subscription files from a JSON configuration. It supports global routing via `happ://` links and multiple VLESS connection strings.

## Features
- **Global Routing**: Encodes your `routing` object into the `happ://routing/onadd/` link.
- **Templates**: Define common Xray-core settings once and reuse them across users.
- **Deep Merge**: User-specific `xray` overrides are merged with templates.
- **Auto Remarks**: Generates readable titles based on country, protocol, and node names.
- **ISO Lookup**: Automatically converts country codes (e.g., `US`) to names and emojis.

## Usage

The tool is designed to be run via the operator script:

```bash
./.operator/scripts/ops/generate-happ-subscriptions <input_json> <output_dir>
```

### JSON Format Specification

The configuration file consists of three main top-level objects:

1.  **`routing`**: Global routing settings (DNS, direct/proxy rules, etc.).
2.  **`templates`**: Named connection templates containing shared protocol and stream settings.
3.  **`users`**: List of users, each with a unique subscription key (`psub`) and a set of configurations derived from templates.

#### Example Reference
See [example.json](./example.json) for a comprehensive template of the configuration format.

## Implementation Details
- **Logic**: Written in Python 3.11 (standard library only).
- **Isolation**: Runs inside a Docker container (`python:3.11-alpine`).
