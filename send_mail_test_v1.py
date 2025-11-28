#!/usr/bin/env python3
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime 

# ===== Gmail 用の設定 =====
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]

FROM_ADDR = GMAIL_USER
TO_ADDR   = os.environ["TO_USER"]  # 自分宛でもOK
# ========================

def main():
    # 現在時刻（ローカル）を "YYYY-MM-DD HH:MM" 形式で
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ★ 件名テンプレ：日付入り
    subject = f"Paperbot cron test - {now_str}"

    # ★ 本文テンプレ：最低限 + 将来ここに結果を差し込める
    body = (
        "This is an automated email from Paperbot ND07 WMK.\n"
        f"Sent at: {now_str}\n"
        "\n"
        "========================\n"
        "(Body will later contain paper search results.)\n"
    )

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        smtp.send_message(msg)

if __name__ == "__main__":
    main()
