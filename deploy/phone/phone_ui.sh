#!/usr/bin/env bash
# 抓取当前界面 UI 树(XML, 含文本与坐标)到宿主机。用法: phone_ui.sh <本地输出.xml>
# 传输层: phone_client.py(phone_ctl); 设备钉定 FOUNDF_ADB_SERIAL 见 phone_client.py。
set -euo pipefail
out="${1:?usage: phone_ui.sh <output.xml>}"
exec python3 "$(dirname "$0")/phone_client.py" ui "$out"
