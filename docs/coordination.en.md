# Multi-device coordination

Devices can use a shared coordinator to agree on selector changes. Each device still runs its own guard and keeps its own configuration and history. The coordinator handles heartbeats and plans; it does not read subscriptions or control devices' Mihomo instances directly.

## How it works

Online participants in a group check and acknowledge the candidate. After a plan is committed, each device rechecks its own selection and service health before switching within the execution window. A manual selection, an unavailable candidate, a device leaving, or an expired confirmation cancels the plan.

Healthy upgrades and recovery require the configured fixed members to be online and agree. If coordination is unavailable, the current selection stays. During a confirmed fault, the affected device can recover locally after rechecking the candidate when coordination fails or times out. A mobile device operates independently while away and joins coordination again when it returns and is online.

When a configured parent selector is chosen manually, an at-home device reports its following status and cancels any pending coordinated plan. While that status is fresh, the other online members may continue healthy upgrades. An expired following status or a disconnected ordinary member is treated as absent. Away devices and preview mode do not contact the coordinator. Leaving the parent starts a new grace period; see [the README](../README.en.md#follow-a-parent-selector-manually) for configuration.

Each device calculates fingerprints from its local node configuration. Fingerprints, not subscription contents or node parameters, are exchanged. Matching fingerprints mean the included parameters match; they do not prove that a provider gives the devices the same public exit IP.

## Prepare the coordinator

Choose a device reachable through an existing SSH connection and place the same project version there. Clients invoke `python3 -m clash_guard.coordination` over SSH for each exchange, using the same local state file. No separate long-running coordinator service is required.

Each client needs non-interactive SSH access and a verified host key. On the coordinator, Python must be able to import `clash_guard`, and the account must be able to write the state file's directory. This host and path are examples:

```json
"command": [
  "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
  "broker-host", "env", "PYTHONPATH=/home/account/clash-party-guard",
  "python3", "-m", "clash_guard.coordination",
  "--state", "/home/account/.local/state/clash-party-guard/broker.json"
]
```

`broker-host` can be an existing SSH alias. If the coordinator also runs a guard, its `command` can invoke the module locally with the same state file. The command is an argument array; the example remote paths contain no spaces. Handle actual paths according to SSH's remote command rules.

## Configure each device

Add `coordination` to each device's own guard configuration. This example is for fixed device `home-a`; `command` is the array shown above:

```json
"coordination": {
  "member": "home-a",
  "group_id": "shared-ai",
  "required_members": ["home-a", "home-b"],
  "fixed_home": true,
  "command": ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "broker-host", "env", "PYTHONPATH=/home/account/clash-party-guard", "python3", "-m", "clash_guard.coordination", "--state", "/home/account/.local/state/clash-party-guard/broker.json"],
  "node_config_path": "/absolute/path/to/local-nodes.json",
  "identity_key_env": "CLASH_COORDINATION_KEY",
  "failure_wait": 20
}
```

See the complete [local coordinator](../examples/coordination-local.example.json), [SSH peer](../examples/coordination-peer.example.json) and [node](../examples/coordination-nodes.example.json) examples. Their hostnames and nodes are fictional and must be replaced before use.

Merge the fragment into a complete JSON configuration and adjust the paths and names:

1. `member` uniquely identifies a device. Devices coordinating the same service use the same `group_id`.
2. `required_members` includes the current device and fixed devices that must be online. With two fixed devices and a laptop, each fixed device uses `["home-a", "home-b"]`; the laptop uses `["home-a", "home-b", "laptop"]`. The laptop joins plans while at home and online; its absence does not permanently block healthy maintenance by the fixed devices.
3. `node_config_path` points to the device's **local** node JSON or YAML file containing a `proxies` array. YAML requires PyYAML. Each device reads its own file; do not commit subscriptions or credentials. Alternatively, `identity_command` can run a local argument array that emits JSON containing `proxies`, which fits an existing node-loading workflow. An advanced `identity_file` maps node names to 64-character lowercase hexadecimal fingerprints.
4. `identity_key_env` optionally names an environment variable. If used, each device needs the same key in its own environment; keep the key and real node data out of public configuration. The shared key creates HMAC fingerprints and does not replace SSH authentication.

If automatic IP quality checks are also enabled with a separate dynamic node source, the two sources must provide matching node configurations. Dynamic sources also bind local check history to node parameters; static identity mappings cannot detect changed parameters under the same name automatically.

Fixed devices use `fixed_home: true`. On a mobile device, remove that field and use the included local gateway detector. Replace the fictional values with the home default gateway's IPv4 and MAC address, and make sure the guard's Python environment can import `clash_guard`:

```json
"presence_command": ["python3", "-m", "clash_guard.presence", "--gateway", "192.0.2.1", "--mac", "02:11:22:33:44:55"]
```

The detector only reads the local default route and neighbor table; it sends no probes. Both values matching means home; a known mismatch means away; missing or conflicting data, command failure or timeout means unknown. It needs an existing gateway neighbor entry and cannot distinguish another network using the same gateway fingerprint. Keep actual addresses and MAC values in local configuration. On Windows, replace `python3` with the Python command available on that device.

While at home, find the gateway and corresponding neighbor entry with these read-only commands: on macOS, `route -n get default` and `arp -an`; on Linux, `ip -4 route show default` and `ip neigh show`; in Windows PowerShell, `Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0` and `Get-NetNeighbor -AddressFamily IPv4`. Read the MAC from the entry for that gateway; if no entry is present, try again later.

You can also use your own `presence_command`, returning `{"home": true}` or `{"home": false}`; `{"home": null}` means unknown. Unknown presence pauses healthy optimization while local fault recovery remains available. The built-in detector has fixture tests for macOS, Windows and Linux output; it has not been validated on real devices for each platform.

`failure_wait` is how many seconds a fault plan waits for coordination. It is not an end-to-end recovery guarantee: failure confirmation and candidate checks also take time.

## Start and inspect

First check each device's local setup using the [README](../README.en.md). `--dry-run` previews local switches and does not contact the coordinator. To run coordination, start each guard with `--enable-switch` and use a separate state directory on each device.

```sh
python3 -m clash_guard --config config.json --enable-switch
```

Watch events such as `coordination_pending`, `coordinated_switch` and `coordination_unavailable`. Verify the coordination result, each device's actual selection and the relevant service separately; a single successful run does not establish long-term stability.
