#!/usr/bin/env bash
# adb 目标设备钉定（source 本文件后使用 "${ADB[@]}" 代替裸 adb）。
# FOUNDF_ADB_SERIAL 可显式钉定设备；未配置时只允许恰好一个在线设备，
# 避免换机后继续使用历史序列，也避免多设备环境误操作。
if [[ -n "${FOUNDF_ADB_SERIAL:-}" ]]; then
  ADB=(adb -s "$FOUNDF_ADB_SERIAL")
else
  mapfile -t _foundf_adb_devices < <(adb devices | awk 'NR > 1 && $2 == "device" {print $1}')
  if [[ "${#_foundf_adb_devices[@]}" -ne 1 ]]; then
    echo "FoundF 要求恰好一个在线 Android 设备，或显式设置 FOUNDF_ADB_SERIAL" >&2
    return 1 2>/dev/null || exit 1
  fi
  ADB=(adb -s "${_foundf_adb_devices[0]}")
  unset _foundf_adb_devices
fi
