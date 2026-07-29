# xray-simple-use

轻量 Xray-core 部署工具，一行命令在 Linux 上跑起 VLESS 代理。

## 依赖

- Python 3.10+ (via [uv](https://docs.astral.sh/uv/))
- Linux x86_64

## 安装

```bash
git clone <this-repo> && cd xray_simple_use
uv python install 3.12  # 如果没有 Python
```

## 使用

```bash
# 1. 下载 xray-core + CloudflareSpeedTest（只需一次）
uv run python -m xray_simple_use.main setup

# 2. 启动代理
uv run python -m xray_simple_use.main start 'vless://uuid@host:port?...'

# 代理可用：SOCKS5 127.0.0.1:10808 | HTTP 127.0.0.1:10809
```

## 常用命令

```bash
# 优选 Cloudflare IP 并重启
uv run python -m xray_simple_use.main optimize --restart

# 测试代理速度（延迟 + 下载）
uv run python -m xray_simple_use.main speedtest

# 查看运行状态
uv run python -m xray_simple_use.main status

# 停止代理
uv run python -m xray_simple_use.main stop
```

## 命令参考

| 命令 | 参数 | 说明 |
|------|------|------|
| `setup` | — | 下载 xray-core 和 CloudflareSpeedTest |
| `start <url>` | `--socks-port` `--http-port` | 解析 VLESS 链接并启动代理 |
| `stop` | — | 停止代理 |
| `status` | — | 查看运行状态 |
| `optimize` | `--url` `--count` `--restart` | Cloudflare IP 优选并更新配置 |
| `speedtest` | `--mode` `--socks-port` | 测试代理连通性/延迟/下载速度 |

## 原理

```
vless:// 链接 → 解析 → Xray JSON 配置 → 启动 xray-core → 本地 SOCKS5/HTTP 代理
                                    ↑
                          CloudflareSpeedTest 优选 IP 替换 address
```
