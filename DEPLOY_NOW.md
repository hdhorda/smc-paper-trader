# Deploy Now — SMC Paper Trading Live (Oracle Cloud Free Tier)

**Time needed: ~45 minutes**  
**Platform: Oracle Cloud Free Tier — Mumbai, 4 OCPU, 24 GB RAM, ₹0 forever**  
**Goal: Strategies live, Telegram alerts on your phone, by 09:15 tomorrow**

---

## STEP 1 — Telegram Bot Setup (10 min)

### 1a. Create the bot
1. Open Telegram on your phone
2. Search for **@BotFather** and open it
3. Send: `/newbot`
4. Bot name → type: `SMC Paper Trader`
5. Username → type: `smc_paper_YOURNAME_bot` (must end in `_bot`, must be unique)
6. BotFather replies with your **Bot Token** — looks like: `7123456789:AABBccDDeeFF...`
7. **Save this token**

### 1b. Get your Chat ID
1. Search for your new bot in Telegram and click **Start**
2. Send it any message (e.g., "hello")
3. Open this URL in your browser (replace YOUR_TOKEN):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
4. Find `"chat":{"id":XXXXXXX}` — that number is your **Chat ID**
5. **Save this number**

### 1c. Test it
Paste this in your browser (replace both values):
```
https://api.telegram.org/botYOUR_TOKEN/sendMessage?chat_id=YOUR_CHAT_ID&text=SMC+Bot+Connected!
```
You should receive "SMC Bot Connected!" in Telegram. ✓

---

## STEP 2 — Push Code to GitHub (10 min)

Oracle's setup script clones your code from GitHub — do this first.

Open **Windows Command Prompt** or **PowerShell**:

```cmd
cd C:\BreezeProjects\Project_8\live_trading

git init
git branch -M main
git add .
git commit -m "Initial commit: SMC Paper Trading System v1.0"
```

Create the GitHub repo:
1. Go to **https://github.com/new**
2. Name: `smc-paper-trader`
3. Set to **Public**
4. **Do NOT** check "Add README"
5. Click **Create repository**

Then push:
```cmd
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/smc-paper-trader.git
git push -u origin main
```

GitHub no longer accepts passwords — use a **Personal Access Token**:
1. Go to: https://github.com/settings/tokens/new
2. Note: `Render deploy token`, Expiry: 90 days, Scope: `repo`
3. Generate → copy immediately
4. Use as password when git asks

Verify at `https://github.com/YOUR_USERNAME/smc-paper-trader` ✓

---

## STEP 3 — Create Oracle Cloud VM (10 min)

> **If you already have an Ubuntu VM running in your Oracle account, skip to Step 4.**

1. Log in at **https://cloud.oracle.com**
2. Go to **Compute → Instances → Create Instance**
3. Fill in:

| Field | Value |
|-------|-------|
| Name | `smc-trader` |
| Region | **ap-mumbai-1** |
| Shape | **VM.Standard.A1.Flex** (click "Change shape" → Ampere → A1.Flex) |
| OCPU | 4 |
| RAM | 24 GB |
| OS | Ubuntu 22.04 |
| SSH Key | Upload your public key (or generate one below) |

**If you don't have an SSH key**, run in PowerShell:
```powershell
ssh-keygen -t ed25519 -C "smc-trader"
# Press Enter 3 times (accept defaults, no passphrase)
# Your public key is at: C:\Users\YOUR_NAME\.ssh\id_ed25519.pub
```
Upload the `.pub` file when Oracle asks for SSH key.

4. Click **Create** — VM takes ~2 min to boot
5. Note the **Public IP address** shown on the instance page

---

## STEP 4 — SSH In and Run Setup Script (10 min)

```powershell
# From Windows PowerShell:
ssh ubuntu@YOUR_ORACLE_IP
```

Once connected, run:
```bash
wget https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/smc-paper-trader/main/live_trading/deploy/oracle_setup.sh
sudo bash oracle_setup.sh
```

This single script:
- Installs Python 3.11, git, gunicorn
- Creates `smcbot` service user
- Clones your GitHub repo
- Sets up Python virtualenv + installs requirements
- Creates blank `.env` file
- Installs systemd service (auto-restart on crash)
- Opens port 5001 in iptables
- Sets up keepalive cron (every 30 min)
- Sets up auto-deploy cron (git pull at 08:00 daily)

**Takes ~5 minutes.**

---

## STEP 5 — Fill In Credentials (5 min)

After setup completes, edit the `.env` file:
```bash
nano /home/smcbot/smc-trader/live_trading/.env
```

Fill in these values:
```env
KITE_API_KEY=your_actual_api_key
KITE_API_SECRET=your_actual_api_secret
KITE_ACCESS_TOKEN=              # leave blank — set daily via /auth
MOCK_MODE=false
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
MAX_OPEN_POSITIONS=15
DAILY_LOSS_CAP=50000
SCAN_WORKERS=4
WARMUP_BARS=2000
LOG_DIR=/home/smcbot/logs
PORT=5001
```

Save: `Ctrl+O`, `Enter`, `Ctrl+X`

Then restart:
```bash
sudo systemctl restart smc-trader
sudo systemctl status smc-trader   # should show "active (running)"
```

---

## STEP 6 — Open Firewall in Oracle Console (5 min)

By default Oracle blocks all inbound traffic at the VCN level. The setup script opens iptables but you must also open it in Oracle's console:

1. Oracle Console → **Networking → Virtual Cloud Networks**
2. Click your VCN → **Security Lists → Default Security List**
3. Click **Add Ingress Rules**
4. Fill in:

| Field | Value |
|-------|-------|
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `5001` |

5. Click **Add Ingress Rules** ✓

Now visit: `http://YOUR_ORACLE_IP:5001`  
You should see the SMC Paper Trading dashboard.

---

## STEP 7 — Daily Token Refresh Workflow

Kite tokens expire at midnight. Before 09:15 each morning:

1. Open in browser: `http://YOUR_ORACLE_IP:5001/auth`
2. Log in with Zerodha credentials
3. Page shows **"Access Token Refreshed"**
4. Engine resumes automatically — no restart needed

**Verify:**
- Dashboard shows `Token OK` badge (green)
- Dashboard shows `WS Connected` badge by 09:15
- Telegram: "WS Connected: N instruments subscribed" message

---

## Verification Checklist

**Tonight:**
- [ ] Telegram test message received ✓ (Step 1c)
- [ ] GitHub repo visible at github.com/YOUR_USERNAME/smc-paper-trader ✓
- [ ] Oracle VM shows "Running" in console ✓
- [ ] SSH works: `ssh ubuntu@YOUR_ORACLE_IP` ✓
- [ ] Setup script completed with no errors ✓
- [ ] `systemctl status smc-trader` shows `active (running)` ✓
- [ ] Dashboard loads at `http://YOUR_ORACLE_IP:5001` ✓
- [ ] Dashboard shows `PAPER` badge (orange) ✓

**Tomorrow morning (09:00):**
- [ ] Visit `/auth` → Kite login → `Token OK` badge turns green ✓
- [ ] By 09:15: `WS Connected` badge appears ✓
- [ ] Telegram: "WS Connected: 27 instruments subscribed" ✓
- [ ] Warmup completes: "Warmup complete: 27/27 symbols OK" in logs ✓
- [ ] Scanning begins at 09:15 ✓

---

## Ongoing: Check Logs

```bash
ssh ubuntu@YOUR_ORACLE_IP
sudo journalctl -u smc-trader -f
```

Common commands:
```bash
sudo systemctl restart smc-trader   # restart engine
sudo systemctl status smc-trader    # check running status
sudo journalctl -u smc-trader -n 50 # last 50 log lines
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Token Expired" on dashboard | Visit /auth, log in with Kite |
| Dashboard not loading | Check `sudo systemctl status smc-trader`; check port 5001 open in VCN |
| SSH connection refused | Wait 2 min after VM boot; check security list has SSH (port 22) ingress rule |
| Setup script fails | Check error message; usually a network issue — re-run `sudo bash oracle_setup.sh` |
| No Telegram messages | Verify TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env; restart service |
| "Warmup fail" in logs | Check KITE_API_KEY and KITE_API_SECRET are correct |

---

## Scaling to 20 Strategies

Oracle Free Tier handles this with room to spare:

| Strategies | Symbols | Threads | RAM used | Oracle Free headroom |
|-----------|---------|---------|----------|----------------------|
| 2 (now) | 27 | 4 | ~300 MB | 23.7 GB free |
| 10 | ~130 | 4 | ~800 MB | 23.2 GB free |
| 20 | ~270 | 8 | ~1.5 GB | 22.5 GB free |

When you add more strategies, just increase `SCAN_WORKERS` in `.env` and restart. No plan upgrade needed.

---

## After First Week

```cmd
# Run on your Windows machine after 15:30 each trading day:
cd C:\BreezeProjects\Project_8
python live_trading/outreach/export_daily_results.py
git add results/ && git commit -m "Results: 2026-MM-DD" && git push
```

Your GitHub will show live paper trading results — crucial for the showcase.
