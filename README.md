# xray-simple-use

轻量 Xray-core 部署工具，一行命令在 Linux 上跑起 VLESS 代理，支持多 IP 自动优选和故障切换。

## 依赖

- Python 3.10+ (via [uv](https://docs.astral.sh/uv/))
- Linux x86_64

## 快速开始

```bash
git clone <this-repo> && cd xray_simple_use

# 1. 创建配置文件
cp config.example.ini config.ini
chmod 600 config.ini
# 编辑 config.ini，填入你的 VLESS 链接

# 2. 下载 xray-core + CloudflareSpeedTest
uv run python -m xray_simple_use.main setup

# 3. 启动守护进程（自动优选 IP + 健康检查 + 故障切换）
uv run python -m xray_simple_use.main run

# 代理可用：SOCKS5 127.0.0.1:10808 | HTTP 127.0.0.1:10809
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `setup` | 下载 xray-core 和 CloudflareSpeedTest |
| `run` | 启动守护进程（推荐） |
| `parse <url>` | 解析 VLESS 链接 |
| `start <url>` | 简单启动（不推荐，用 run 代替） |
| `stop` | 停止 xray |
| `status` | 查看运行状态 |
| `speedtest` | 测试代理连通性/延迟/下载速度 |

## 运行逻辑

```
启动 → 读缓存队列 → 启动 Xray → 后台更新候选
       ↓
每 30s 健康检查 → 连续失败 → 切换下一 IP
       ↓                           ↓
  仅剩 2 个 IP → 后台补充候选    仅剩 1 个 → 紧急扫描
       ↓
  每天凌晨 5 点 → 全量重扫
```

## 配置文件

默认路径：`~/.config/xray-simple-use/config.ini`

| 段 | 参数 | 说明 | 默认值 |
|----|------|------|--------|
| `[server]` | `vless_url` | VLESS 分享链接 | — |
| `[daemon]` | `daily_scan_hour` | 每日全量扫描时间 | 5 |
| | `health_interval` | 健康检查间隔(秒) | 30 |
| | `failure_threshold` | 连续失败触发切换次数 | 3 |
| | `cooldown_seconds` | IP 故障冷却时间 | 600 |
| | `replenish_threshold` | 剩余可用 IP 数触发补充 | 2 |
| `[cfst]` | `concurrency` | CFST 并发数 | 30 |
| | `attempts` | 每 IP 测试次数 | 2 |
| | `candidate_count` | 队列保留候选数 | 5 |
| | `skip_download` | 跳过下载测速 | true |
| `[test]` | `attempts` | 真实链路测试次数 | 3 |
| | `timeout_seconds` | 请求超时 | 5 |
