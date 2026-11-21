#!/bin/sh
cd "$(dirname "$0")" || exit 1
export XMODIFIERS="${XMODIFIERS:-@im=ibus}"
state="$HOME/.local/share/roller-derby-replay"
mkdir -p "$state" || exit 1
exec /usr/bin/python3 -u multi_monitor.py "$@" >> "$state/multi-monitor-stdout.log" 2>&1
