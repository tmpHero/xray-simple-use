# xray-simple-use

轻量 Xray-core 部署工具，支持多 IP 自动优选、健康检查和故障切换。

**依赖**：Python 3.10+ / uv / Linux x86_64 / curl

> **注意**：当前 IP 切换会重启 Xray，正在进行的下载、上传、SSH 等连接会中断。

支持 `vmess://` 和 `vless://` 两种分享链接格式，自动识别。

## 快速开始

```bash
git clone <this-repo> && cd xray_simple_use

# 1. 从 v2rayN 复制分享链接，写入配置文件
cp config.example.ini config.ini
chmod 600 config.ini
nano config.ini    # 修改 [server] vless_url

# 2. 下载依赖（国内服务器用 --mirror）
uv run python -m xray_simple_use.main setup --mirror

# 3. 后台启动
uv run python -m xray_simple_use.main run

# 代理可用：SOCKS5 127.0.0.1:10808 | HTTP 127.0.0.1:10809
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `setup` | 下载 xray-core 和 CloudflareSpeedTest |
| `run` | 后台启动守护进程 |
| `run -f` | 前台运行（调试用，Ctrl+C 停止） |
| `log` | 查看实时日志 |
| `stop` | 停止 xray |
| `status` | 查看运行状态 |
| `stability-test` | 24h 稳定性测试（上线前验证） |
| `speedtest` | 测试代理连通性/延迟/下载速度 |

## 运行逻辑

```
启动 → 读缓存队列 → 启动 Xray → 后台更新候选
       ↓
每 30s 健康检查 → 连续 3 次失败 → 切换到队列下一 IP
       ↓                            ↓
  仅剩 2 个 IP → 后台补充        仅剩 1 个 → 紧急扫描
       ↓
  每天凌晨 5 点 → 全量重扫（CFST 粗筛 → 真实链路测试 → 更新队列）
```

## 配置文件

路径优先级：`--config` > `~/.config/xray-simple-use/config.ini` > `./config.ini`

| 段 | 参数 | 说明 | 默认值 |
|----|------|------|--------|
| `[server]` | `vless_url` | 分享链接（vmess:// 或 vless://） | — |
| `[daemon]` | `daily_scan_hour` | 每日全量扫描时间 | 5 |
| | `health_interval` | 健康检查间隔(秒) | 30 |
| | `failure_threshold` | 连续失败触发切换次数 | 3 |
| | `cooldown_seconds` | IP 故障冷却时间(秒) | 600 |
| | `replenish_threshold` | 剩余可用数触发补充 | 2 |
| | `emergency_threshold` | 剩余可用数触发紧急 | 1 |
| `[cfst]` | `concurrency` | CFST 并发线程数 | 30 |
| | `attempts` | 每 IP 测试次数 | 2 |
| | `candidate_count` | 队列保留候选数 | 5 |
| | `skip_download` | 跳过下载测速 | true |
| | `max_latency_ms` | 最大延迟阈值 | 500 |
| | `max_loss_rate` | 最大丢包率 | 0.0 |
| `[test]` | `attempts` | 真实链路测试次数(≥3) | 3 |
| | `timeout_seconds` | 请求超时(秒) | 5 |
| | `probe_url` | 探测地址 | gstatic.com/generate_204 |
| | `expected_http_status` | 期望 HTTP 状态码 | 204 |

