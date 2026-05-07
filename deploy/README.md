# finafeed Collector — VPS 部署指南

## 环境要求

- Ubuntu 22.04+ (推荐)
- Python 3.12+
- 网络可访问 `fstream.binance.com` 和 `fapi.binance.com`

## 快速部署

### 1. 创建目录

```bash
# 直接使用当前 ubuntu 用户，无需创建专用用户
sudo mkdir -p /opt/finafeed
sudo chown ubuntu:ubuntu /opt/finafeed
```

### 2. 上传代码

```bash
# 从本地上传 finafeed 目录到 VPS
scp -r ./finafeed/ user@your-vps:/opt/finafeed/
```

### 3. 安装 uv 并同步依赖

```bash
# 安装 uv (如果还没装)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 同步依赖 (自动创建 .venv)
cd /opt/finafeed/finafeed
uv sync
```

### 4. 配置

编辑 `/opt/finafeed/collector/config.yaml`

关键配置：

- `symbols`: 要监控的交易对
- 配置telegram bot，用写入env的方法: `FINAFEED_TELEGRAM_TOKEN`, `FINAFEED_TELEGRAM_CHAT`; 或者在config.yaml中配置

获取 Chat ID：给你的 bot 发送 `/start`，然后访问：`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`

### 5. 验证配置 (Dry Run)

```bash
cd /opt/finafeed/finafeed
PYTHONPATH=/opt/finafeed uv run python -m finafeed.main --dry-run
```

⚠️ 此时也可直接启用: `uv run python -m finafeed.main`

### 6. 安装 systemd 服务

```bash
sudo cp /opt/finafeed/finafeed/deploy/finafeed.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable finafeed
sudo systemctl start finafeed
```

### 7. 创建数据和日志目录（权限）

```bash
mkdir -p /opt/finafeed/data /opt/finafeed/logs
```

## 日常运维

### 查看状态

```bash
sudo systemctl status finafeed
```

### 查看日志

```bash
# systemd journal 日志
sudo journalctl -u finafeed -f

# 结构化 JSON 日志 (可用 jq 过滤)
tail -f /opt/finafeed/finafeed/logs/finafeed.log | jq .

# 只看错误
tail -f /opt/finafeed/finafeed/logs/finafeed.log | jq 'select(.level == "error")'

# 只看大户多空比采集日志
tail -f /opt/finafeed/finafeed/logs/finafeed.log | jq 'select(.event == "LONG_SHORT_RATIO")'
```

### 重启

```bash
sudo systemctl restart finafeed
```

### 停止

```bash
sudo systemctl stop finafeed
```

### 查看 Prometheus 指标

```bash
curl http://localhost:17895/metrics
```

## 数据管理

### 查看数据库大小

```bash
ls -lh /opt/finafeed/finafeed/data/
```

### 查询数据

```bash
sqlite3 /opt/finafeed/finafeed/data/finafeed.db

# 爆仓数量
SELECT COUNT(*) FROM liquidations;

# 最近 10 条爆仓
SELECT * FROM liquidations ORDER BY event_time DESC LIMIT 10;

# 未平仓数量
SELECT COUNT(*) FROM open_interest;

# 大户多空比记录数
SELECT COUNT(*) FROM long_short_ratio;
```

### 备份数据库

```bash
# 使用 SQLite 的在线备份 (不影响写入)
sqlite3 /opt/finafeed/finafeed/data/finafeed.db ".backup /tmp/finafeed_backup.db"

# 或者 rsync 到本地
rsync -avz user@your-vps:/opt/finafeed/finafeed/data/ ./backup/
```

## 环境变量覆盖

以下环境变量可覆盖 config.yaml 中的值：

| 变量                      | 说明                                |
| ------------------------- | ----------------------------------- |
| `FINAFEED_SYMBOLS`        | 逗号分隔的 symbol 列表              |
| `FINAFEED_DB_PATH`        | 数据库文件路径                      |
| `FINAFEED_LOG_LEVEL`      | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `FINAFEED_METRICS_PORT`   | Prometheus 端口                     |
| `FINAFEED_TELEGRAM_TOKEN` | Telegram Bot Token                  |
| `FINAFEED_TELEGRAM_CHAT`  | Telegram Chat ID                    |

在 systemd 中使用：

```bash
sudo systemctl edit finafeed
```

添加：

```ini
[Service]
Environment=FINAFEED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

## 故障排查

### 连接不上币安

```bash
# 测试网络连通性
curl -s https://fapi.binance.com/fapi/v1/ping
# 应返回 {}

# 测试 WebSocket
python3 -c "import asyncio, aiohttp; asyncio.run((lambda: print('ok'))())"
```

### 数据库锁定

SQLite WAL 模式下极少出现，如果遇到：

```bash
sqlite3 /opt/finafeed/data/finafeed.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 内存过高

检查 Prometheus 指标中的 `process_resident_memory_bytes`，正常应 < 100MB。
