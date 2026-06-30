# Oracle Cloud Deployment Guide

## Target: Oracle Cloud Free Tier (Always Free)
- **Region**: ap-mumbai-1 (Mumbai) — lowest latency to NSE
- **Shape**: VM.Standard.A1.Flex — 4 OCPU, 24 GB RAM, 200 GB storage
- **OS**: Ubuntu 22.04 LTS
- **Cost**: ₹0 forever (Oracle Always Free)

---

## One-Time Setup

### 1. Create Oracle Cloud Account
1. Go to https://cloud.oracle.com and sign up (credit card needed for verification, not charged)
2. Select **Always Free** tier
3. Choose region: **ap-Mumbai-1**
4. Create VM: Compute → Instances → Create Instance
   - Shape: VM.Standard.A1.Flex (4 OCPU, 24GB)
   - OS: Ubuntu 22.04
   - Add your SSH public key

### 2. Connect and Run Setup
```bash
ssh ubuntu@YOUR_SERVER_IP
wget https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/live_trading/deploy/oracle_setup.sh
sudo bash oracle_setup.sh
```

### 3. Set Kite Credentials
```bash
nano /home/smcbot/smc-trader/live_trading/.env
# Fill in KITE_API_KEY, KITE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
sudo systemctl restart smc-trader
```

### 4. Open Firewall (Oracle Console)
1. Networking → Virtual Cloud Networks → Your VCN
2. Security Lists → Default Security List
3. Add Ingress Rule: TCP, Port 5001, Source 0.0.0.0/0

---

## Daily Routine (5 min before 09:15)

| Time  | Action |
|-------|--------|
| 09:00 | Visit `http://SERVER_IP:5001/auth` in browser |
| 09:05 | Log in with Kite — token auto-set |
| 09:10 | Dashboard shows "Token OK", "WS Connected" |
| 09:15 | System begins scanning automatically |
| 15:00 | All open positions hard-closed automatically |

---

## Service Management

```bash
# Check status
sudo systemctl status smc-trader

# View live logs
sudo journalctl -u smc-trader -f

# Restart engine
sudo systemctl restart smc-trader

# Stop for maintenance
sudo systemctl stop smc-trader
```

---

## Auto-Heal Features
- **WebSocket drops**: auto-reconnects with 5→10→20→40→60s backoff
- **Token expiry**: pauses engine, resumes after /auth refresh
- **Server crash**: systemd restarts within 10s (max 5 restarts per minute)
- **Keepalive**: cron hits /api/health every 30 min (prevents Oracle VM reclaim)
- **Git auto-deploy**: pulls latest code at 08:00 daily and restarts

---

## Upgrade Path (15+ strategies, real money)
When scaling to 15+ strategies with real money:
1. Upgrade Oracle shape to VM.Standard3.Flex (paid, ~₹2,000/month)
2. Migrate SQLite → PostgreSQL (install via `apt install postgresql`)
3. Add Redis for signal deduplication across workers
4. Set up Nginx reverse proxy with SSL for dashboard security
5. Enable dashboard basic auth (see README)
