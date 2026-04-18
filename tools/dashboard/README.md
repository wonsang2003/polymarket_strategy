# Polymarket Weather Dashboard

Streamlit dashboard for monitoring the live paper/real trading system.
Read-only view on `weather.db` — safe to run alongside the pipeline.

## What it shows

- KPI strip (open positions, today P&L, cumulative P&L, signals today)
- Open positions (from `trade_history WHERE outcome IS NULL`)
- Equity curve of settled trades + win-rate panel
- Recent settlements with per-trade P&L
- Calibration health — fitted distributions per `(city, model, lead)` with auto-flagging of `|μ| > 5` or `σ > 5` outliers (the ones `strategy.py` excludes at inference)
- Recent forecast errors by city (line chart, last 60 days)
- Lag monitor panel (if `tools/lag_monitor/logs/events.jsonl` exists)
- Walk-forward panel (if `tools/walk_forward/last_run.csv` exists — write it with `python tools/walk_forward/backtest.py --all-cities --csv tools/walk_forward/last_run.csv`)

## Quick start (local only)

```bash
pip install streamlit pandas
streamlit run tools/dashboard/app.py
# → opens http://localhost:8501
```

That's it for local use.

---

## Remote access via Tailscale

**Why Tailscale and not port-forwarding your router:** exposing port 8501 through your home router puts your DB + Streamlit debug surface on the public internet. Tailscale gives you a private WireGuard mesh — the dashboard only answers requests from devices that share your tailnet (your phone, laptop, iPad). No DDNS, no port forward, no cert provisioning.

### 1. Install Tailscale on your Mac (where the pipeline runs)

```bash
# Homebrew is the cleanest option
brew install --cask tailscale
open -a Tailscale
# click the menu bar icon → "Log in" → log in with your identity provider
```

After login, note the machine's tailnet IP (`100.x.y.z`) or its MagicDNS name (e.g. `my-mac.tail-scale.ts.net`) from the menu bar.

### 2. Install Tailscale on your phone/laptop

Same account. That's it. Now both devices see each other at `100.x.y.z`.

### 3. Bind Streamlit to the tailnet interface

```bash
# Option A: bind to all interfaces (simplest)
streamlit run tools/dashboard/app.py \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    --server.enableCORS false \
    --browser.gatherUsageStats false

# Option B: bind ONLY to the tailnet IP (safer — refuses local LAN)
streamlit run tools/dashboard/app.py \
    --server.address 100.x.y.z \
    --server.port 8501 \
    --browser.gatherUsageStats false
```

Pick Option B once you have the IP memorized. Binding to `0.0.0.0` also answers requests from any device on your LAN (coffee shop WiFi, your roommate's laptop); binding to the tailnet IP only answers devices inside your tailnet.

### 4. Open it from your phone

- `http://my-mac.tail-scale.ts.net:8501` (MagicDNS — recommended)
- `http://100.x.y.z:8501` (raw IP — works if MagicDNS is off)

### 5. Keep it running

The dashboard is just a Python process — if your terminal closes, it dies. Three options, in order of sophistication:

**Option 1: `nohup`** — fine for testing

```bash
nohup streamlit run tools/dashboard/app.py \
    --server.address 100.x.y.z --server.port 8501 \
    > ~/dashboard.log 2>&1 &
```

**Option 2: `launchd` (macOS-native)** — starts at login, auto-restarts on crash

Create `~/Library/LaunchAgents/com.wonsang.polymarket-dashboard.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wonsang.polymarket-dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/streamlit</string>
        <string>run</string>
        <string>/ABS/PATH/polymarket_strat/tools/dashboard/app.py</string>
        <string>--server.address</string>
        <string>100.x.y.z</string>
        <string>--server.port</string>
        <string>8501</string>
        <string>--browser.gatherUsageStats</string>
        <string>false</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/ABS/PATH/polymarket_strat</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/polymarket-dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/polymarket-dashboard.err</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.wonsang.polymarket-dashboard.plist
launchctl start com.wonsang.polymarket-dashboard
# tail logs
tail -f /tmp/polymarket-dashboard.log
# unload (to edit plist or turn off)
launchctl unload ~/Library/LaunchAgents/com.wonsang.polymarket-dashboard.plist
```

**Option 3: Docker** — overkill for a single Streamlit app; skip.

### 6. Auth (optional, only if you want defense in depth)

Tailscale itself is the primary auth — only tailnet devices reach port 8501. If you want an additional password on top:

```bash
pip install streamlit-authenticator
```

Add at the top of `app.py`:

```python
import streamlit_authenticator as stauth
authenticator = stauth.Authenticate({"usernames": {"wonsang": {"name": "Wonsang",
    "password": stauth.Hasher(["your-password"]).generate()[0]}}},
    "polymarket-dashboard", "abc", cookie_expiry_days=30)
authenticator.login()
if not st.session_state.get("authentication_status"):
    st.stop()
```

Honest take: if your tailnet key is safe, this is redundant. Skip it.

---

## Troubleshooting

**Dashboard shows "Database not found"**
Run calibration first: `polymarket-strat weather-calibrate --cities seoul`.

**"disk I/O error" on read-only connection**
SQLite `mode=ro` requires the file to already exist. Either calibrate once, or temporarily flip the DB_PATH in `app.py` to a local copy.

**Streamlit won't bind to `100.x.y.z`**
Wait 30s after `open -a Tailscale` for the interface to come up. Verify with `ifconfig | grep 100.`.

**Phone can't reach the dashboard**
- Is Tailscale enabled on the phone? (VPN icon should be green.)
- Check `tailscale status` on the Mac — both devices should show "active".
- macOS firewall may block incoming on 8501. System Settings → Network → Firewall → allow `streamlit`/Python.

**Dashboard is slow / stale**
Data is cached 60s (`@st.cache_data(ttl=60)`). Click the 3-dot menu → "Clear cache" or hard-refresh.

---

## Why this stack

- **Streamlit** > Flask/FastAPI/React for a single-dev read-only dashboard. Zero frontend code, auto-reloads on save, native pandas integration.
- **Tailscale** > nginx + Let's Encrypt + port forward. Zero certs, zero DNS, zero open ports on your router. Identity-based auth via your OIDC provider.
- **SQLite `mode=ro`** > running a second DB copy. Live pipeline keeps writing with WAL; dashboard reads a consistent snapshot via the WAL read marker. No contention.
