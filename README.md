# xray-simple-use

轻量 Xray-core 部署工具，支持多 IP 自动优选、健康检查和故障切换。

**依赖**：Python 3.10+ / uv / Linux x86_64 / curl

> **注意**：当前 IP 切换会重启 Xray，正在进行的下载、上传、SSH 等连接会中断。

---

## 快速开始

```bash
git clone <this-repo> && cd xray_simple_use

# 1. 从 v2rayN 复制 VLESS 链接，写入配置文件
cp config.example.ini config.ini
chmod 600 config.ini
nano config.ini    # 修改 [server] vless_url

# 2. 下载 xray-core 和 CloudflareSpeedTest（只需一次）
uv run python -m xray_simple_use.main setup

# 3. 启动守护进程
uv run python -m xray_simple_use.main run
```

**代理地址**：SOCKS5 `127.0.0.1:10808` / HTTP `127.0.0.1:10809`

---

## 命令参考

| 命令 | 说明 |
|------|------|
| `setup` | 下载 xray-core 和 CloudflareSpeedTest |
| `run` | **启动守护进程**（后台优选 + 健康检查 + 故障切换） |
| `stop` | 停止 xray |
| `status` | 查看运行状态 |
| `stability-test` | **24h 稳定性测试**（上线前验证） |
| `speedtest` | 测试代理连通性/延迟/下载速度 |
| `parse <url>` | 解析 VLESS 链接，查看配置 |
| `start <url>` | 简单启动（不推荐，用 `run` 代替） |

---

## 生产前稳定性测试

部署到服务器后，先跑 24 小时测试：

```bash
# 终端1：启动 daemon
uv run python -m xray_simple_use.main run

# 终端2：跑稳定性测试
uv run python -m xray_simple_use.main stability-test --duration 24h
```

测试内容：

| 周期 | 内容 |
|------|------|
| 每 10s | HTTP 代理延迟探测（主+备地址） |
| 每 5min | 5MB 下载测速 |
| 每 1min | xray 内存/存活/队列变化检测 |

测试结束后输出报告：

```
24-hour stability report
Latency probes    8640
Successful        8629
Failed            11
Availability      99.87%
Latency P50       181 ms
Latency P95       264 ms
Download tests    288
IP switches       1
Xray crashes      0
Result            PASS
```

日志输出到 `logs/stability-YYYY-MM-DD.jsonl`，Ctrl+C 提前停止也会输出部分报告。

---

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

### 故障处理

| 可用 IP | 状态 | 动作 |
|---------|------|------|
| 5~3 | 正常 | 仅切换 |
| 2 | 预警 | 切换 + 后台补充候选 |
| 1 | 紧急 | 切换 + 立即 CFST |
| 0 | 全失效 | 逐个探测 fallback + 紧急 CFST |

---

## 配置文件

路径优先级：`--config` > `~/.config/xray-simple-use/config.ini` > `./config.ini`

| 段 | 参数 | 说明 | 默认值 |
|----|------|------|--------|
| `[server]` | `vless_url` | VLESS 分享链接 | — |
| `[daemon]` | `daily_scan_hour` | 每日全量扫描时间 | 5 |
| | `health_interval` | 健康检查间隔(秒) | 30 |
| | `failure_threshold` | 连续失败触发切换次数 | 3 |
| | `cooldown_seconds` | IP 故障冷却时间(秒) | 600 |
| | `replenish_threshold` | 剩余可用数触发补充 | 2 |
| | `emergency_threshold` | 剩余可用数触发紧急 | 1 |
| `[cfst]` | `concurrency` | CFST 并发线程数 | 30 |
| | `attempts` | 每 IP 测试次数 | 2 |
| | `candidate_count` | 队列保留候选数 | 5 |
| | `skip_download` | 跳过下载测速(`-dd`) | true |
| | `max_latency_ms` | 最大延迟阈值 | 500 |
| | `max_loss_rate` | 最大丢包率 | 0.0 |
| `[test]` | `attempts` | 真实链路测试次数(≥3) | 3 |
| | `timeout_seconds` | 请求超时(秒) | 5 |
| | `probe_url` | 探测地址 | gstatic.com/generate_204 |
| | `expected_http_status` | 期望 HTTP 状态码 | 204 |

---

## 文件说明

| 文件 | 说明 | 提交 Git |
|------|------|----------|
| `config.example.ini` | 配置模板 | 是 |
| `config.ini` | 用户配置（含凭据） | 否 |
| `queue.json` | IP 队列/排序/冷却状态 | 否 |
| `config.json` | Xray 运行配置（含 UUID） | 否 |
| `xray.log` | Xray 运行日志 | 否 |
| `xray.pid` | Xray 进程 PID | 否 |
| `logs/` | 稳定性测试日志 | 否 |
| `third_party/` | xray-core + cfst 二进制 | 否 |

## 项目结构

```
xray_simple_use/
├── main.py              # CLI 入口
├── daemon.py            # 守护主循环（健康检查/故障切换/定时扫描）
├── config.py            # config.ini 加载与校验
├── vless.py             # VLESS 链接解析 + Xray 配置生成
├── xray.py              # Xray 进程管理（启停/重启/PID 验证）
├── cf_speedtest.py      # CloudflareSpeedTest 调用/过滤/干扰检测
├── tester.py            # 候选 IP 并发真实链路测试
├── speedtest.py         # 统一代理探测 probe_socks()
├── queue.py             # IP 优先级队列持久化/熔断/排序
└── stability_test.py    # 24h 稳定性测试
```
