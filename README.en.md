# Clash Party Guard

[中文](README.md) · [English](README.en.md)

Monitor a Clash Party or Mihomo proxy group and switch to a working candidate when the selected node keeps failing. Brief interruptions leave the current selection alone.

The guard uses the Python standard library and talks to the Mihomo controller. You can use it as a standalone tool or adapt the checks and switching policy to your setup.

## Run the demo

Python 3.9 or later is required. No third-party packages are needed:

```sh
python3 examples/demo.py
```

The demo uses fictional nodes and simulated results. It shows how a failure leads to a suggested switch without connecting to a controller or the internet.
The final `would_switch` event indicates a proposed switch; the demo does not change any selection.

## Features

- Check the selected node against configured service endpoints without switching on a brief failure.
- Check candidates after failures meet both the count and duration thresholds, then recheck before switching.
- Respect manual selections.
- Configure the group, service URLs, expected statuses, excluded node names and check intervals.
- Preview proposed changes without changing the selection.
- Record check results and compare success rates and latency after a holding period; upgrade when a candidate stays better and passes verification.
- Return to the original primary node after sustained recovery and verification.
- Compare optional locally supplied quality data or enable background IP quality checks. Third-party scoring is off by default.
- Optionally coordinate node changes across devices at home.
- Optionally follow a parent selector chosen manually, with a fresh grace period after leaving it.

## Connect to Clash Party

1. Enable the local Mihomo HTTP controller in Clash Party and identify its address, the proxy group and two HTTPS endpoints on different hosts. The group must be a `Selector` with a node selected directly.
2. Copy `examples/config.json` to `config.json` and replace the example endpoints and expected statuses. Set `health_url` to exactly the URL used by the node health checks; candidates need a result from the last five minutes. If the controller requires authentication, put the secret in an environment variable and set `secret_env` to its name.
3. Start with preview mode:

   ```sh
   python3 -m clash_guard --config config.json --dry-run
   ```

   Preview mode contacts the controller, tests the configured endpoints and records local history, but does not switch nodes. To inspect the group without probing or writing history, run:

   ```sh
   python3 -m clash_guard --config config.json --read-only
   ```

4. When the results look right, enable switching:

   ```sh
   python3 -m clash_guard --config config.json --enable-switch
   ```

The guard checks continuously while it is running. Press `Ctrl+C` to stop it. By default, state is stored under `~/.local/state/clash-party-guard/` and separated by selector. Preview and switching use separate directories, so history and automatic scores accumulated in preview do not carry over. Use `--state-dir` to set a directory; only one guard can use it at a time. If you set it explicitly, use different directories for preview and switching. The holding period, sampling thresholds and recovery time are configurable.

To use locally supplied quality data, set `quality_file` in your configuration to a local JSON file. See the [quality data example](examples/quality.example.json): `score` ranges from 0 to 100, and `observed_at` is the actual measurement time. Replace the example timestamp. Data is valid for 24 hours by default. Candidates with the same success rate are compared by score only if each has a valid score; a healthy upgrade requires a score at least as high as the current node only when both scores are valid.

You can also configure [automatic IP quality checks](docs/ip-quality.en.md) to sample locally and query third-party scores, or [multi-device coordination](docs/coordination.en.md) to coordinate changes with devices at home. Both features require separate configuration and are disabled by default. If both `quality_file` and automatic checks are configured, enabling automatic checks uses only automatic scores; it does not fall back to the manual file.

The [Party override example](examples/party-override.js) adds nodes listed in `proxies` to a named group. Change the group name and filter before use; configurations using providers need further adaptation.

## Follow a parent selector manually

To choose another selector in Clash Party, first make it an option in the guarded selector. Then list the permitted parent names at the top level of the guard configuration:

```json
"follow_parent_groups": ["Parent Selector"]
```

Each parent must be a `Selector`. You can also edit `followParentGroups` in the [Party override example](examples/party-override.js) to add valid parents as options; it does not select one automatically. Once a parent is chosen manually, the guard pauses probes and automatic selection for this group, cancels any running quality sample and pauses sampling. A missing or cyclic parent path produces `follow_parent_unavailable`. After leaving the parent, sampling can resume, and the guard resets its primary node and starts the `startup_grace` observation period. See [coordination](docs/coordination.en.md) for multi-device behavior.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

The tests use a simulated controller and do not connect to your existing Clash configuration.

## Current scope

When automatic scoring or coordination uses a dynamic node source (`node_config_path` or `identity_command`), the guard checks local history against node parameters. If a node changes or disappears, its old records are archived and its former-primary eligibility is cleared. When identity is temporarily unknown, healthy upgrades and returns to the former primary are paused; fault recovery remains available after two checks. If multiple dynamic sources are configured, their node configurations must agree.

Without a dynamic node source, check history is associated by name and cannot detect changed parameters under the same name. Static `node_ids` and `identity_file` cannot detect such changes automatically either. Manually supplied quality data is also associated by name. Multi-device coordination and automatic scoring have been tested in isolation, but have not yet passed end-to-end tests across real devices and external scoring services. Real application performance depends on the network and selected nodes.

See [CONTRIBUTING.md](CONTRIBUTING.md) to report a problem or contribute. Released under the [MIT License](LICENSE).
