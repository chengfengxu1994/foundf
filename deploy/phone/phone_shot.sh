#!/usr/bin/env bash
# 手机截图并拉到宿主机。用法: phone_shot.sh <本地输出.png>
# 传输层: phone_client.py(phone_ctl); 设备钉定 FOUNDF_ADB_SERIAL 见 phone_client.py。
set -euo pipefail
out="${1:?usage: phone_shot.sh <output.png>}"
exec python3 "$(dirname "$0")/phone_client.py" shot "$out"
