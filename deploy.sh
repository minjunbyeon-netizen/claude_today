#!/bin/bash
cd /opt/daily-focus
git pull origin master
.venv/bin/pip install -q -r requirements.txt
systemctl restart daily-focus
echo "배포 완료"
