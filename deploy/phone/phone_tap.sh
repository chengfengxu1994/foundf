#!/usr/bin/env bash
# 手机点按。用法: phone_tap.sh <x> <y>  (物理像素坐标, 1220x2712)
# 传输层: phone_client.py(phone_ctl); 设备钉定 FOUNDF_ADB_SERIAL 见 phone_client.py。
set -euo pipefail
exec python3 "$(dirname "$0")/phone_client.py" tap "${1:?x}" "${2:?y}"
