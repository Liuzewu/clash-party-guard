# 多设备协同

多台设备可以通过同一个协调端协商策略组切换。每台设备仍运行自己的守护程序、保留自己的配置和检测历史；协调端只处理心跳与切换计划，不读取订阅或直接操作各设备的 Mihomo。

## 工作方式

同一组内，在线的参与设备会检查候选并确认；计划提交后，各设备再次核对本机选择和业务状态，再在执行窗口内切换。用户手动选择、候选失效、设备离开或确认过期时，计划会取消。

健康升级或恢复回切需要固定成员在线并完成确认；协调不可用时保持现线。确认故障时，如果协调失败或等待超时，故障设备可在重新验证后独立恢复，不要求其他设备跟随。移动设备在外出时独立运行，返家且在线后再次参与协商。

配置并手动选中父组时，设备在家会向协调端报告跟随状态，并取消进行中的协同计划。状态有效期间，其他在线成员可继续健康升级；跟随状态过期或普通成员失联时，仍按缺席处理。离家和预览模式不联系协调端。退出跟随后进入新的启动观察期，配置方法见 [README](../README.md#手动跟随父组)。

节点身份由本机配置计算成指纹，跨设备交换指纹，不传订阅和节点参数。身份指纹一致仅表示参与计算的参数一致，不能证明服务商给不同设备分配的公网 IP 相同。

## 准备协调端

选一台可通过现有 SSH 访问的设备作为协调端，在那里放置相同版本的项目。客户端每次通过 SSH 执行 `python3 -m clash_guard.coordination`，使用同一个本地状态文件；不需要单独运行常驻协调服务。

各客户端需要能免交互登录协调端，且应事先确认 SSH 主机指纹。协调端的 Python 须能找到 `clash_guard` 模块，并有权限写入状态文件所在目录。下面的主机名和路径只是示例：

```json
"command": [
  "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
  "broker-host", "env", "PYTHONPATH=/home/account/clash-party-guard",
  "python3", "-m", "clash_guard.coordination",
  "--state", "/home/account/.local/state/clash-party-guard/broker.json"
]
```

`broker-host` 可以是已配置的 SSH 别名。协调端自己也运行守护时，可以将其 `command` 改为本机命令，并指向同一状态文件。命令以参数数组填写；示例路径不含空格，实际路径须按 SSH 远端命令的规则处理。

## 配置各设备

在每台设备自己的守护配置中加入 `coordination`。例如，下方是固定设备 `home-a` 的配置片段，`command` 使用上面的数组：

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

完整配置可参考[协调端示例](../examples/coordination-local.example.json)、[SSH 设备示例](../examples/coordination-peer.example.json)和[节点示例](../examples/coordination-nodes.example.json)。示例中的地址与节点均为虚构值，使用前须替换。

将片段合并进完整 JSON 配置后，修改各项路径和名称：

1. `member` 是设备的唯一名称；参与同一服务的设备使用相同 `group_id`。
2. `required_members` 包含该设备自身和该组必须在线的固定设备。以两台固定设备加一台笔记本为例，固定设备各填 `["home-a", "home-b"]`，笔记本填 `["home-a", "home-b", "laptop"]`。笔记本在家且在线时会加入计划，离家不会永久阻塞固定设备的健康维护。
3. `node_config_path` 指向**本机**节点 JSON 或 YAML，其中应有 `proxies` 数组。使用 YAML 需要安装 PyYAML。不同设备分别读取自己的文件，不要把包含订阅和凭据的文件复制到仓库。也可用 `identity_command` 配置本机命令，输出含 `proxies` 的 JSON，适合已有节点加载流程；高级用法可提供 `identity_file`，内容为节点名到 64 位小写十六进制指纹的映射。
4. `identity_key_env` 是可选的环境变量名。若使用，各设备须在各自环境中提供相同的密钥；密钥和真实节点信息不要写入公开配置。共同密钥用于计算 HMAC 指纹，不能代替 SSH 身份验证。

如果还启用了自动 IP 品质检测，并为两项功能分别配置了动态节点来源，两处来源必须提供一致的节点配置。动态来源也用于核对本地检测历史，静态身份映射不能自动发现同名节点的参数变化。

固定设备用 `fixed_home: true`。移动设备删除该字段，可使用项目提供的本地网关识别程序。例如，将示例地址和 MAC 替换为自己家中默认网关的 IPv4 地址和 MAC，并在运行守护程序的环境中确保 Python 能找到 `clash_guard`：

```json
"presence_command": ["python3", "-m", "clash_guard.presence", "--gateway", "192.0.2.1", "--mac", "02:11:22:33:44:55"]
```

程序只读取本机默认路由与邻居表，不主动发送探测。两项均匹配为在家；已知不匹配为外出；数据缺失、冲突、命令失败或超时为未知。该方法依赖邻居表已有网关记录，也不能防止其他网络使用相同的网关指纹。真实地址和 MAC 只保留在本机配置中。Windows 可将 `python3` 换成实际的 Python 启动命令。

在家中可用以下只读命令查找默认网关和对应邻居记录：macOS 使用 `route -n get default` 和 `arp -an`；Linux 使用 `ip -4 route show default` 和 `ip neigh show`；Windows PowerShell 使用 `Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0` 和 `Get-NetNeighbor -AddressFamily IPv4`。从对应网关的邻居条目读取 MAC；没有记录时稍后重试。

也可继续使用自己的 `presence_command`，输出 `{"home": true}` 或 `{"home": false}`；`{"home": null}` 代表未知。未知时健康优化保守停止，故障仍可本地恢复。内置识别程序的 macOS、Windows 和 Linux 输出已用模拟数据测试，尚未逐平台实机验收。

`failure_wait` 是故障方案等待协同时的秒数。它不是从故障开始到切换完成的保证时间，还包括故障确认及备用验证。

## 启动与检查

先按 [README](../README.md) 分别检查每台设备的本机配置。`--dry-run` 会预览本机切换，不联系协调端；要运行协同，各设备需要以 `--enable-switch` 启动，并使用各自的状态目录。

```sh
python3 -m clash_guard --config config.json --enable-switch
```

可观察程序输出中的 `coordination_pending`、`coordinated_switch` 和 `coordination_unavailable` 等事件。协调结果、各机实际节点和对应业务连通应分别核验；单次成功不能代表长期稳定。
