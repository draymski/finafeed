# finafeed

7×24 Binance Futures data collection daemon — liquidations, open interest, and long/short ratio, stored in SQLite with automatic rotation.

## Features

- **WebSocket** liquidation stream with exponential-backoff reconnect
- **REST polling** for open interest (5s, value-dedup) and long/short ratio (≈41h)
- **SQLite WAL** storage with 800 MB file rotation
- **Prometheus** metrics on `:17895`
- **Telegram** alerts + interactive `/stat` command
- Structured JSON logging via `structlog`
- Graceful shutdown on `SIGTERM` / `SIGINT`

## Structure

```
finafeed/
├── finafeed/            # Python package
│   ├── main.py          # Entry point & lifecycle
│   ├── config.py / config.yaml
│   ├── storage.py       # SQLite WAL async storage
│   ├── collectors/
│   │   ├── ws_liquidation.py
│   │   ├── rest_open_interest.py
│   │   └── rest_long_short.py
│   └── infra/
│       ├── logger.py
│       ├── metrics.py
│       ├── alerter.py
│       └── telegram_bot.py
├── deploy/
│   ├── finafeed.service # systemd unit
│   └── README.md        # VPS deployment guide
├── data/                # SQLite databases (runtime)
└── logs/                # JSON logs (runtime)
```

## Quick Start

```bash
# Install dependencies
uv sync

# Validate config and create DB schema
uv run python -m finafeed.main --dry-run

# Run
uv run python -m finafeed.main
```

## Configuration

Edit `finafeed/config.yaml`. Key fields:

```yaml
symbols:
  - BTCUSDT
  - ETHUSDT

alert:
  enabled: true
  telegram:
    bot_token: "<your-bot-token>"
    chat_id:   "<your-chat-id>"
```

Environment variable overrides (useful for secrets):

| Variable | Description |
|---|---|
| `FINAFEED_SYMBOLS` | Comma-separated symbol list |
| `FINAFEED_TELEGRAM_TOKEN` | Telegram bot token |
| `FINAFEED_TELEGRAM_CHAT` | Telegram chat ID |
| `FINAFEED_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `FINAFEED_DB_PATH` | Database file path |

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/stat` | Row-count delta per symbol per table since last call, plus DB size |

## Deploy

See [`deploy/README.md`](deploy/README.md) for VPS deployment with systemd.


## commit history

- 版本`[optim] OI使用5min降采样`保持了OI的5s轮询频率, 修改了入库频率为`5min`. 后续发现数据过于稀疏, 决定生产环境回退使用`8976fd4`版本.