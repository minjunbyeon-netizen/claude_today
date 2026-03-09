#!/bin/bash
set -e

# SSH key
mkdir -p ~/.ssh
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGCHQ9zBH0MAo3TL1b6NoAwAl3o+ArWY6MUKNVM3K7WM USER@PC1000" >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys

# Python & git
apt-get update -qq && apt-get install -y -q python3-pip python3-venv git

# Clone or update
if [ -d /opt/daily-focus ]; then
  cd /opt/daily-focus && git pull
else
  git clone https://github.com/minjunbyeon-netizen/claude_today.git /opt/daily-focus
fi

# Virtualenv & deps
cd /opt/daily-focus
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# Systemd service
cat > /etc/systemd/system/daily-focus.service << 'EOF'
[Unit]
Description=Daily Focus
After=network.target

[Service]
WorkingDirectory=/opt/daily-focus
ExecStart=/opt/daily-focus/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable daily-focus
systemctl restart daily-focus

echo "=============================="
echo "배포 완료! 포트 8080에서 실행 중"
echo "=============================="
