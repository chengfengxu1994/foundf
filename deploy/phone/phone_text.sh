#!/usr/bin/env bash
# 输入文本。用法: phone_text.sh '文本'
# 传输层已切换 phone_client.py(phone_ctl): adb input text 只支持 ASCII,
# 中文输入需手机端安装 ADBKeyBoard 后另行广播(ASCII 限制继承自 phone_ctl)。
set -euo pipefail
text="${1:?usage: phone_text.sh <text>}"
exec python3 "$(dirname "$0")/phone_client.py" text "$text"
