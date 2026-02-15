# SignalBot Architecture

## Purpose
Signal-only trading assistant that monitors Hyperliquid markets and sends Telegram alerts for:
- LONG/SHORT signals (BOS + Retest + Acceptance)
- TP1 / TP2 hits
- Stop invalidations
No auto-execution (yet).

## Runtime
- Host: AWS Lightsail (Ubuntu)
- Process manager: systemd
- Each symbol runs as a separate service instance:
  - signalbot@BTC
  - signalbot@ETH
  - signalbot@SOL
  - etc.

## Components

### 1) signalbot.py (Signal Engine)
- Connects to Hyperliquid WS (`allMids`)
- Builds candles:
  - 5m execution timeframe
  - 1h bias timeframe (aggregated from 5m closes)
- Computes indicators:
  - EMA fast/slow (defaults 9/21)
  - Pivots for swing high/low (PIVOT_L)
  - ATR for retest buffer (ATR_LEN, RETEST_BUF_ATR)
- Logic:
  - Determine bias using 1h EMA trend
  - Confirm execution trend using 5m EMA trend
  - Detect BOS on 5m close through last swing
  - Wait for retest with ATR buffer
  - Require ACCEPT_BARS closes for acceptance
- Sends Telegram alerts via notify()

### 2) notifier.py
- Telegram send helper
- Uses env vars:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID

### 3) control.py (Telegram Control Bot)
- Telegram bot that can start/stop/restart/status symbol services
- Talks to systemd via subprocess calls:
  - systemctl start signalbot@BTC
  - systemctl stop signalbot@BTC
- Uses sudoers rule (NOPASSWD) to allow systemctl commands.

### 4) logger.py
- Minimal logging mode (TRADE/DEBUG)
- Logs to stdout -> journalctl

## Environment / Config

### .env (server)
- ENV=mainnet
- TELEGRAM_BOT_TOKEN=...
- TELEGRAM_CHAT_ID=...
- CONTROL_SYMBOLS=BTC,ETH,SOL,xyz:GOLD,...

### config.py
- Strategy defaults (TF_SECONDS, EMA periods, pivot length, buffers, etc.)
- WS endpoints for mainnet/testnet

## Services

### signalbot template unit
File: /etc/systemd/system/signalbot@.service
- Loads env: /home/ubuntu/SignalBot/.env
- SYMBOL provided by instance name (%i)
- Restart=always

Start/stop examples:
- sudo systemctl start signalbot@BTC
- sudo systemctl stop signalbot@BTC
- sudo systemctl restart signalbot@BTC
- sudo systemctl status signalbot@BTC

Logs:
- journalctl -u signalbot@BTC -f

### control bot service
File: /etc/systemd/system/signalbot-control.service
- Runs control.py
- Restart=always

Logs:
- journalctl -u signalbot-control -f

## Deployment
- Repo: GitHub (private/public)
- Typical update flow:
  1. Edit locally
  2. git commit + push
  3. ssh into server
  4. git pull
  5. sudo systemctl restart signalbot@BTC (or all symbols)

## Security Notes
- Tokens stored in .env on server (not committed)
- GitHub access via deploy key
- sudoers allows restricted systemctl actions for signalbot services only

## Future Enhancements
- Add heartbeat notifier / health monitoring
- Add auto-execution (Hyperliquid API) behind a feature flag
- Add risk manager + max open signals per symbol
- Add web dashboard
- Add Docker + CI deploy
