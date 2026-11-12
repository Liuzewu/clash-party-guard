# Automatic IP quality checks

IP quality checks are optional. When enabled, the guard samples candidates in the background using a separate Mihomo process. It reads the exit IP seen by each configured service through that candidate, then queries a third-party `trust_score` for each address. The lowest score is used as a comparison signal.

## Prepare the node file

You need a working Mihomo binary, `curl`, and a local node configuration containing a `proxies` array. JSON and YAML are supported; reading YAML additionally requires PyYAML. Node fields match Mihomo's `proxies` configuration. This example shows the JSON shape only; it is not a usable proxy:

```json
{
  "proxies": [
    {
      "name": "Node A",
      "type": "ss",
      "server": "proxy.example.net",
      "port": 443,
      "cipher": "aes-128-gcm",
      "password": "YOUR_PASSWORD"
    }
  ]
}
```

Names must match the nodes in the Mihomo selector. For nodes from a separate provider, `identity_command` can invoke a local loader that emits JSON containing a `proxies` array; use either `identity_command` or `node_config_path`, not both. The source is read before sampling and again before saving a result. Nodes that depend on `dialer-proxy` or external plugins cannot be reproduced independently and are skipped.

The node file can contain credentials: keep it local and do not commit it to a public repository. Checks disclose the candidate's exit IP to the requested services. By default the sampler reads the ChatGPT and Claude trace endpoints, then sends each resulting IP to Net.Coffee. Sampling uses a separate process and temporary loopback listening port. The cache stores hashes of the node configuration and exit addresses, timestamps and scores, rather than raw IP addresses or node settings.

## Enable the sampler

Add `ip_quality` to your guard configuration and use paths on the device:

```json
"ip_quality": {
  "enabled": true,
  "node_config_path": "/absolute/path/to/private-nodes.json",
  "core_path": "/absolute/path/to/mihomo",
  "interval_seconds": 21600,
  "quality_max_age": 86400,
  "request_timeout_seconds": 8
}
```

To use a local loader, replace `node_config_path` with `"identity_command": ["python3", "/absolute/path/to/node-loader.py"]`. The command must print JSON containing a `proxies` array to standard output.

When automatic checks are enabled, only scores bound to node configuration are used. Even if `quality_file` is set, the guard does not fall back to that file when an automatic score is missing or expired.

Escape backslashes as required by JSON for Windows paths. Configure the controller, selector and service checks as described in the [README](../README.en.md), then run:

```sh
python3 -m clash_guard --config config.json --dry-run
```

Preview mode probes services and collects scores but does not switch nodes. Once the output matches your configuration, use `--enable-switch`; the two modes use separate state directories by default, so scores are collected again during switching. `--read-only` reads the controller without probing services. Automatic sampling considers only healthy, non-excluded individual nodes, at most one at a time. `--once` uses available scores but does not start a new sample; nodes are sampled progressively during continuous operation.

## Results and settings

The cache is `ip-quality.json` in the state directory. By default, a node may be sampled again after six hours, and a score is valid for 24 hours. Transient network failures back off for 15 minutes, one hour, then six hours; other failures typically wait six hours. Timeouts, HTTP 403, missing fields and expired data remain unknown. When changing the lifetime, set both `ip_quality.quality_max_age` and the top-level `quality_max_age`; the shorter value applies, in seconds.

`trace_urls` accepts a mapping of 1–4 names to HTTPS URLs. Each URL must return text containing an `ip=...` line. `lookup_base` can point to a compatible HTTPS endpoint that accepts an IP appended to its path and returns JSON containing a numeric `trust_score` from 0 to 100. Set `curl_path` if necessary.

These scores come from a third party, not from an OpenAI or Anthropic account assessment. A changed node configuration invalidates the old score. A changed exit IP is detected on the next sample; once detected, the old score is withdrawn until a new score is obtained. Account login and real application performance require their own checks.
