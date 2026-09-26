#!/bin/bash
# Local reproduction of the ACTUAL upstream rt_threads selector; all commands
# it uses are stubbed. No /proc scheduler access, systemctl or D-Bus execution.
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck disable=SC1091
source "$base/../frame-stream-audio-fix.sh" >/dev/null
systemctl() { printf '10\n'; }
mock_cls=RR
ps() { printf '11 %s 80\n' "$mock_cls"; }
before=$(rt_threads)
test -n "$before"
declare -A demoted=([10:11]=80)
mock_cls=TS   # ps representation of SCHED_OTHER after upstream's demotion
unset demoted
declare -A demoted=()   # a replacement process starts without the old memory
after=$(rt_threads)
test -z "$after"
test "${#demoted[@]}" -eq 0
printf '%s\n' 'REPRODUCED: actual upstream rt_threads ignores demoted SCHED_OTHER; new demoted map is empty.'
