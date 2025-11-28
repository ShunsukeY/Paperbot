#!/bin/bash

# このスクリプト(run_mail.sh)が置いてあるディレクトリを取得
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 同じディレクトリにある .gmail_env を読み込む
. "$SCRIPT_DIR/.gmail_env"

# 同じディレクトリにある Python スクリプトを実行
exec /usr/bin/python3 "$SCRIPT_DIR/send_mail_test_v3.py"
