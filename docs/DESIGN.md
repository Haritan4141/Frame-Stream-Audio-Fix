# Design and changes from upstream

Baseline: `69669be81ee3571125ef3ff8a771523b5138c13b` of Proxian/Frame-Stream-Audio-Fix. MIT notices and original source are retained.

## Findings addressed

1. Original restore_all discards state even when restoration fails. This candidate removes an individual record only after identity and the full scheduling state match.
2. Original code saves RT priority but not full policy/flags/nice/identity. This candidate saves those before mutation.
3. Original uninstall removes files without verifying successful stop/restoration. This candidate gates removal on verified recovery, inactive service and zero unresolved records; failures retain recoverable files.
4. Original demoted map exists only in memory. SIGKILL skips traps, and Restart=always starts with an empty map; rt_threads selects only RR/FIFO, not already demoted OTHER. A durable write-ahead journal and startup recovery address known completed mutations.

Purpose remains stream detection every two seconds and handling the same three audio user services. Persistence, strict schema, identity checks, failure injection and Linux getters are implemented with a small Bash entrypoint plus Python standard library, not a minimal line-count patch. No extra audio pipeline, network service, driver or auto-updater is added.

## RealtimeKit boundary

Primary references:
- [D-Bus interface](https://sources.debian.org/src/rtkit/0.14-1/org.freedesktop.RealtimeKit1.xml)
- [Implementation](https://sources.debian.org/src/rtkit/0.14-1/rtkit-daemon.c/)
- [Linux scheduler policies](https://man7.org/linux/man-pages/man7/sched.7.html)
- [proc stat identity fields](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)

The referenced RTKit implementation makes realtime threads RR plus RESET_ON_FORK. It cannot restore arbitrary FIFO/policy flags by accepting them as an argument. High-priority requests can set OTHER plus RESET_ON_FORK and nice; partial effects are possible before failure. Therefore only original RR|RESET, rtprio 1–80 and nice 0–19 are eligible. Restore nice in the non-privileged increasing direction, request RR, then verify all saved values. Actual device RTKit compatibility is untested; do not claim exactly factory SteamOS state.

## Failure semantics

- Persist original state before mutation; private 0700 directory/0600 files, schema/checksum, atomic replace, fsync, flock. An anchor prevents silently treating a missing initialized state as empty.
- Identity includes boot, UID, PID/TID, both start ticks, executable identity, names and service. Mismatch or vanished thread means NO_TOUCH, retain state.
- Mark DEMOTE_IN_FLIGHT/RESTORE_IN_FLIGHT before D-Bus. A timeout or nonzero client status cannot prove the server cancelled a request. Keep these records and fail closed even if one snapshot matches the original.
- TRACKED recovery precedes enumeration/demotion. Verified records alone are cleared.
- Safety warnings exit 4 and do not restart automatically; real crashes have bounded on-failure restarts. ExecStopPost attempts recovery, but explicit uninstall verification remains mandatory.
- Uninstall archives operational files instead of irreversibly deleting them. Reload/removal verification failure restores files and reloads again; a failed rollback reload is reported without discarding files.
- Previous-boot acknowledgment requires inactive service, a different boot for every record, exact state hash and explicit acknowledgment. It archives all state bytes before clearing active records and reports NOT_RESTORED, never recovery success. There is no same-boot bypass or reboot function.

Do not race other scheduler tools. Numeric PID/TID APIs retain a final precheck-to-operation race. Filesystem failure, deliberate same-user modification, hard-killed uninstall and real audio quality remain residual risks.

## Evidence scope

The local 34-test fault-injection harness uses synthetic identities and fake scheduler/RTKit/systemd. A real local child is killed without cleanup; its fake world persists so another process recovers from durable state. Linux CI additionally exercises POSIX local filesystem operations, not privileged or real audio operations. Private real-device logs and identities are intentionally not included.
