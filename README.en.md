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
- Use optional, locally supplied node quality data when comparing candidates, without querying a third-party scoring service.

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

The guard checks continuously while it is running. Press `Ctrl+C` to stop it. Check history is stored under `~/.local/state/clash-party-guard/` by default. The holding period, sampling thresholds and recovery time are configurable.

To use external quality data, set `quality_file` in your configuration to a local JSON file. See the [quality data example](examples/quality.example.json): `score` ranges from 0 to 100, and `observed_at` is the actual measurement time. Replace the example timestamp. Data is valid for 24 hours by default. Candidates with the same success rate are compared by score only if each has a valid score; a healthy upgrade requires a score at least as high as the current node only when both scores are valid. You supply the scores; the project does not fetch or verify them.

The [Party override example](examples/party-override.js) adds nodes listed in `proxies` to a named group. Change the group name and filter before use; configurations using providers need further adaptation.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

The tests use a simulated controller and do not connect to your existing Clash configuration.

## Current scope

This version runs on one device; multi-device coordination and automatic IP scoring are not included. Check history and local quality data are associated by node name, so changing a node's connection parameters does not automatically reset old records. Tests run in isolation; the quality of a real connection depends on the network and selected nodes.

See [CONTRIBUTING.md](CONTRIBUTING.md) to report a problem or contribute. Released under the [MIT License](LICENSE).
