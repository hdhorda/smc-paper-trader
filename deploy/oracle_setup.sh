#!/bin/bash
# oracle_setup.sh — One-time Oracle Cloud (Ubuntu 22.04) server setup
# Run as: sudo bash oracle_setup.sh
# Target: Oracle Cloud Free Tier — VM.Standard.A1.Flex (4 OCPU, 24GB RAM, Mumbai ap-mumbai-1)
# ─────────────────────────────────────────────────────────────────────────────────

set -e
echo "=== SMC Paper Trading — Oracle Cloud Setup ==="
echo ""

# Prompt for GitHub details
if [ -z "$GITHUB_USERNAME" ]; then
    read -p "Enter your GitHub username (e.g. hardikdhorda): " GITHUB_USERNAME
fi
if [ -z "$GITHUB_REPO" ]; then
    read -p "Enter your GitHub repo name [smc-paper-trader]: " GITHUB_REPO
    GITHUB_REPO=${GITHUB_REPO:-smc-paper-trader}
fi
REPO_URL="https://github.com/${GITHUB_USERNAME}/${GITHUB_REPO}.git"
echo "Cloning from: $REPO_URL"
echo ""

# 1. System update
apt-get update -y && apt-get upgrade -y

# 2. Python 3.11 + pip
apt-get install -y python3.11 python3.11-venv python3-pip git curl unzip

# 3. Create service user (non-root for security)
id -u smcbot &>/dev/null || useradd -m -s /bin/bash smcbot
echo "smcbot ALL=(ALL) NOPASSWD:/bin/systemctl restart smc-trader" >> /etc/sudoers.d/smcbot

# 4. Clone repository
cd /home/smcbot
if [ ! -d "smc-trader" ]; then
    git clone "$REPO_URL" smc-trader
fi
chown -R smcbot:smcbot /home/smcbot/smc-trader

# 5. Python virtual environment
cd /home/smcbot/smc-trader
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 6. Create .env file (EDIT BEFORE RUNNING)
cat > /home/smcbot/smc-trader/.env << 'ENV_EOF'
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_ACCESS_TOKEN=
MOCK_MODE=false
PORT=5001
LOG_DIR=/home/smcbot/logs
MAX_OPEN_POSITIONS=15
DAILY_LOSS_CAP=50000
SCAN_WORKERS=4
WARMUP_BARS=2000
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENV_EOF
chown smcbot:smcbot /home/smcbot/smc-trader/.env
chmod 600 /home/smcbot/smc-trader/.env

# 7. Create log directory
mkdir -p /home/smcbot/logs
chown -R smcbot:smcbot /home/smcbot/logs

# 8. Install systemd service
cp /home/smcbot/smc-trader/deploy/smc-trader.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable smc-trader
systemctl start smc-trader
echo "systemd service installed and started."

# 9. Oracle VCN firewall — open port 5001 for dashboard
# (Also configure in Oracle Cloud Console: Networking > VCN > Security Lists > add port 5001)
iptables -I INPUT -p tcp --dport 5001 -j ACCEPT
apt-get install -y iptables-persistent
netfilter-persistent save
echo "Port 5001 opened in iptables."

# 10. Hourly keepalive cron (prevents Oracle from reclaiming idle VMs)
(crontab -u smcbot -l 2>/dev/null; echo "*/30 * * * * curl -s http://localhost:5001/api/health > /dev/null 2>&1") | crontab -u smcbot -
echo "Keepalive cron installed (hits /api/health every 30 min)."

# 11. Git pull + restart cron (auto-deploy on push)
(crontab -u smcbot -l 2>/dev/null; echo "0 8 * * * cd /home/smcbot/smc-trader && git pull && sudo systemctl restart smc-trader") | crontab -u smcbot -
echo "Auto-deploy cron installed (git pull + restart at 08:00 daily)."

echo ""
echo "=== Setup complete ==="
echo "Edit /home/smcbot/smc-trader/.env with your Kite credentials."
echo "Then run: sudo systemctl restart smc-trader"
echo "Dashboard: http://$(curl -s ifconfig.me):5001"
echo ""
echo "Daily token refresh workflow:"
echo "  1. Visit http://SERVER_IP:5001/auth before 09:15"
echo "  2. Log in with Kite"
echo "  3. Token auto-set — engine resumes"
