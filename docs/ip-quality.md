# 自动检测 IP 品质

自动检测是可选功能。开启后，守护程序会在后台轮流检查候选节点：启动独立的 Mihomo 进程，通过该节点读取各目标服务看到的出口 IP，再查询第三方的 `trust_score`。多个目标返回的分数取较低值，作为节点比较的参考。

## 准备

需要可运行的 Mihomo 和 `curl`，以及一份只在本机保存、含 `proxies` 数组的节点配置。支持 JSON 和 YAML；读取 YAML 需要额外安装 PyYAML。节点字段与 Mihomo 的 `proxies` 配置一致。例如，下面只展示 JSON 结构，不能直接连接：

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

节点名称需要与 Mihomo 策略组中的名称一致。如果节点来自单独的 provider，可使用 `identity_command` 调用本机加载程序，输出含 `proxies` 数组的 JSON；`identity_command` 与 `node_config_path` 二选一。每次采样前和提交结果前都会重读来源。程序无法独立复现依赖 `dialer-proxy` 或外部插件的节点，这类节点会跳过评分。

节点文件可能包含凭据，保留在本机，不要提交到公开仓库。检测时会向对应服务发送该节点的出口 IP：默认先访问 ChatGPT 和 Claude 的 trace，再将取得的 IP 发往 Net.Coffee 查询。检测使用独立进程与本机临时监听端口，缓存记录节点配置和出口的哈希、时间及评分，不保存明文 IP 或节点参数。

## 开启

在自己的守护配置中加入 `ip_quality`，把路径换成本机实际路径：

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

如果使用本机加载程序，用 `"identity_command": ["python3", "/absolute/path/to/node-loader.py"]` 替换 `node_config_path`；程序须将包含 `proxies` 数组的 JSON 写到标准输出。

启用自动检测时，只使用与节点配置绑定的自动评分；即使同时设置了 `quality_file`，自动评分缺失或失效时也不会回退到手动文件。

路径为 Windows 路径时按 JSON 规则转义反斜杠。其他检测目标、策略组和本机控制器仍按 [README](../README.md) 配置，然后正常启动守护程序：

```sh
python3 -m clash_guard --config config.json --dry-run
```

预览模式会做网络检测并积累评分，但不会切换节点。确认结果后，可使用 `--enable-switch` 运行；两种模式默认使用不同状态目录，正式运行会重新积累评分。`--read-only` 只读取控制器，不做业务探测。自动采样只选择当前基础健康、未被排除的具体节点，每次最多采样一个。`--once` 只使用已有评分，不启动自动采样；持续运行时才会逐步采样。

## 查看和调整

评分缓存位于状态目录的 `ip-quality.json`。默认每个节点六小时后可再次采样，评分有效期为 24 小时；临时网络错误依次等待 15 分钟、1 小时和 6 小时，其他失败通常等待 6 小时。超时、403、字段缺失和失效数据均视为未知。自定义有效期时，同时调整 `ip_quality.quality_max_age` 和配置顶层的 `quality_max_age`，实际有效期取两者中较短的值，单位为秒。

`trace_urls` 可设置 1–4 个名称到 HTTPS URL 的映射；每个 URL 须返回包含 `ip=...` 行的文本。`lookup_base` 可指定兼容接口的 HTTPS 地址，接口需接受附在路径后的 IP，并返回包含 0–100 数值型 `trust_score` 的 JSON。可通过 `curl_path` 指定 `curl`。

检测结果来自第三方，不能当作 OpenAI 或 Anthropic 的官方账号评分。节点参数变化后，旧评分会失效。出口 IP 变化须等下次采样发现；发现变化后会撤销旧评分，直至取得新评分。实际账号登录与业务体验仍需要自行验证。
