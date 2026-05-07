# LiquiTrack Collector — VPS 部署指南

## 环境要求

- Ubuntu 22.04+ (推荐)
- Python 3.12+
- 网络可访问 `fstream.binance.com` 和 `fapi.binance.com`

## 快速部署

### 1. 创建用户和目录

```bash
sudo useradd -r -s /bin/false liquitrack
sudo mkdir -p /opt/liquitrack
sudo chown liquitrack:liquitrack /opt/liquitrack
```

### 2. 上传代码

```bash
# 从本地上传 collector 目录到 VPS
scp -r ./collector/ user@your-vps:/opt/liquitrack/
```

### 3. 安装 uv 并同步依赖

```bash
# 安装 uv (如果还没装)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 同步依赖 (自动创建 .venv)
cd /opt/liquitrack/collector
uv sync
```

### 4. 配置

编辑 `/opt/liquitrack/collector/config.yaml`：

```bash
nano /opt/liquitrack/collector/config.yaml
```

关键配置：
- `symbols`: 要监控的交易对
- `alert.telegram.bot_token`: Telegram Bot Token
- `alert.telegram.chat_id`: 你的 Telegram Chat ID

获取 Chat ID：给你的 bot 发送 `/start`，然后访问：
```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

### 5. 验证配置 (Dry Run)

```bash
cd /opt/liquitrack/collector
PYTHONPATH=/opt/liquitrack uv run python -m collector.main --dry-run
```

### 6. 安装 systemd 服务

```bash
sudo cp /opt/liquitrack/collector/deploy/liquitrack-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable liquitrack-collector
sudo systemctl start liquitrack-collector
```

### 7. 创建数据和日志目录（权限）

```bash
sudo mkdir -p /opt/liquitrack/collector/data /opt/liquitrack/collector/logs
sudo chown -R liquitrack:liquitrack /opt/liquitrack/
```

## 日常运维

### 查看状态

```bash
sudo systemctl status liquitrack-collector
```

### 查看日志

```bash
# systemd journal 日志
sudo journalctl -u liquitrack-collector -f

# 结构化 JSON 日志 (可用 jq 过滤)
tail -f /opt/liquitrack/collector/logs/collector.log | jq .

# 只看错误
tail -f /opt/liquitrack/collector/logs/collector.log | jq 'select(.level == "error")'

# 只看大户多空比采集日志
tail -f /opt/liquitrack/collector/logs/collector.log | jq 'select(.event == "LONG_SHORT_RATIO")'
```

### 重启

```bash
sudo systemctl restart liquitrack-collector
```

### 停止

```bash
sudo systemctl stop liquitrack-collector
```

### 查看 Prometheus 指标

```bash
curl http://localhost:17895/metrics
```

## 数据管理

### 查看数据库大小

```bash
ls -lh /opt/liquitrack/collector/data/
```

### 查询数据

```bash
sqlite3 /opt/liquitrack/collector/data/liquitrack.db

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
sqlite3 /opt/liquitrack/collector/data/liquitrack.db ".backup /tmp/liquitrack_backup.db"

# 或者 rsync 到本地
rsync -avz user@your-vps:/opt/liquitrack/collector/data/ ./backup/
```

## 环境变量覆盖

以下环境变量可覆盖 config.yaml 中的值：

| 变量 | 说明 |
|------|------|
| `LIQUITRACK_SYMBOLS` | 逗号分隔的 symbol 列表 |
| `LIQUITRACK_DB_PATH` | 数据库文件路径 |
| `LIQUITRACK_LOG_LEVEL` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `LIQUITRACK_METRICS_PORT` | Prometheus 端口 |
| `LIQUITRACK_TELEGRAM_TOKEN` | Telegram Bot Token |
| `LIQUITRACK_TELEGRAM_CHAT` | Telegram Chat ID |

在 systemd 中使用：
```bash
sudo systemctl edit liquitrack-collector
```
添加：
```ini
[Service]
Environment=LIQUITRACK_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
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
sqlite3 /opt/liquitrack/collector/data/liquitrack.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 内存过高

检查 Prometheus 指标中的 `process_resident_memory_bytes`，正常应 < 100MB。
