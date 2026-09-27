# Frame Stream Audio Fix — unofficial hardened fork

**Unofficial, experimental, temporary workaround. Not affiliated with Valve or endorsed by upstream.** Remove it after an official fix, using verified restoration first.

Upstream: [Proxian/Frame-Stream-Audio-Fix](https://github.com/Proxian/Frame-Stream-Audio-Fix).

- Audited upstream baseline: `69669be81ee3571125ef3ff8a771523b5138c13b`.
- Upstream script SHA-256: `ee3963c30579ac9ab9002a6503e776b72e2de8a321d9f4224c5f8bcba4f83eb9`.
- `main` is reserved for upstream history; hardened changes live on `hardened` or explicitly reviewed `hardened-*` branches. Git cannot store both a `hardened` ref and nested `hardened/*` refs.
- Original MIT license and copyright are retained unchanged in [LICENSE](LICENSE). Fork additions are MIT licensed too.
- The root `frame-stream-audio-fix.sh` is the unchanged upstream baseline for comparison/tests. **Do not run its install/uninstall commands.** The candidate is under `hardened/`.

## Status and limits

Deployed runtime commit: **`69f8e9dcd9199f881fb40bb37178635f559f8ce9`**. On 2026-09-27 it passed all 15 short real-device acceptance gates and was explicitly enabled. The `hardened` branch was fast-forwarded to this runtime commit without changing `main` or rewriting history. This later documentation revision does not change or redeploy the runtime.

**Acceptance state: `AUDIO_FIX_ENABLED_READY_FOR_USER_LONG_TEST`.** Boot Autostart is **VERIFIED / PASS** after an operator's normal reboot. Read-only verification found the user service enabled/active/running, no automatic restarts, zero pending restoration records, all three audio services active, and no current-boot PipeWire SIGKILL or Fix WARNING/GUARD_BLOCKED/UNRESOLVED entries. Sleep/Resume is **VERIFIED / PASS within retrospective log and service-health scope**: retained logs show suspend/resume and no new relevant warning/SIGKILL after the accepted runtime was enabled. This is not a live audio-quality or streaming-during-suspend test.

The short trial independently measured the stable three realtime workers and a newly created priority-60 audio-filter thread changing to OTHER|RESET / priority 0 / nice -11 while alive. The transient then ended and was archived as `RETIRED_EXITED_TRACKED_THREAD` (not restored); stream end restored the stable workers to RR|RESET / priority 80 / nice 0. All 13 baseline thread identities/schedulers matched, active state was empty and three retirement archives were retained. The stop test used the explicitly approved one-time post-stop recovery fallback because the short-lived ExecStopPost exit metadata was unavailable; the fallback exited 0 with state/scheduler/archive unchanged.

Long-duration VRChat/SteamVR/PCVR testing is **IN PROGRESS / NOT YET PASS**. Do not treat the short acceptance, boot test or local CI as a cure for audio dropouts. Prior blocked trials and migration evidence remain in the private deployment record. New installation, replacement, service control or evidence cleanup still requires separate approval; CI does not contact a headset. See [lifecycle semantics and review](docs/TRANSIENT-LIFECYCLE.md) and [publication policy](docs/PUBLICATION.md).

The workaround detects this user's PC stream (`vrlink`) and temporarily requests high-priority normal scheduling for eligible threads of the three audio user services (`pipewire`, `pipewire-pulse`, `wireplumber`). A PipeWire SIGKILL log alone does not prove its sender or exact cause. This workaround cannot guarantee the absence of all SIGKILL causes.

## Hardening

| Area | Hardened behavior |
| --- | --- |
| Original state | Policy including flags, RT priority, nice, UID, PID/TID, process/thread start ticks, names, executable path/device/inode and boot ID. |
| Durable recovery | Save **before** mutation: strict JSON/checksum, private files, atomic replacement, file/directory fsync, single-writer lock. Recovery precedes new demotion. |
| Policy | Own approved executables only; RR plus RESET_ON_FORK, original priority 1–80 and nice 0–19. FIFO, unknown policies and unsupported state are skipped with warnings. |
| Verified restore | For a surviving identical thread, restore nice and RR and verify identity/policy/priority/nice. Unexpected state remains blocked. |
| Transient lifecycle | Known-completed TRACKED or pre-dispatch WRITE_AHEAD records with affirmative same-boot exit/reuse proof are archived before retirement. Retirement is NOT restoration. Ambiguous identity, read failures and IN_FLIGHT never auto-retire. |
| Crash handling | TRACKED records survive a crash. Interrupted/timed-out D-Bus requests stay IN_FLIGHT: no blind retry or false success from one matching snapshot. |
| Uninstall | Stop, restore/verify, inactive, zero unresolved state, disable/recheck, archive files, reload/verify. Ordinary failure restores moved files and reloads again. |
| Service | User unit; on-failure restart, exit-4 restart prevention, rate limit, ExecStopPost recovery, restrictive umask. |

The upstream in-memory map loses restoration data after an untrappable termination. Its RR/FIFO selector cannot rediscover demoted OTHER threads. Tests reproduce this with the **actual upstream selector and stubs**. See [design/differences](docs/DESIGN.md).

No sudo/root, readonly disable, package installation, system-directory changes, SSH authentication changes, Developer Mode changes, Steam/Valve file modifications, telemetry or credential access. Persistent files stay in the user's home. RealtimeKit D-Bus is local, not external network communication.

## Pinned review and installation

**Never install from a moving main or branch URL.** Use an independently reviewed full 40-character commit SHA, fetch/check out that exact commit, inspect the diff and verify `hardened/SHA256SUMS` before any device changes. A branch name or hash manifest alone is not proof of trust.

See [INSTALL-REVIEW.md](docs/INSTALL-REVIEW.md) for the non-overwriting fresh-install/removal review plan, not an upgrade command for an existing deployment. There is no automatic installer, updater or deploy workflow. Further installation and real-device tests require separate approval.

Runtime bytes match the pinned, deployed runtime commit; launcher hash alone is insufficient. A documentation-only commit is not a new deployment approval:

| File in hardened/ | SHA-256 |
| --- | --- |
| frame_audio_guard.py | c7f754fe50663afc02c8ae3c756a97b827617fdc4ecf223c0e1c6356cb23de97 |
| frame-stream-audio-fix | 7b92792640f06bb1c947009ace668282dbf483e209a76b15945f4bff7a870b25 |
| frame-stream-audio-fix.service | 334cce38b707178e1268b4cba5c38b21a3426f443602fde8a82684a3e936e909 |

## Tests / CI

Python 3.12, standard library only. From the repository root:

```sh
python -B scripts/check_public_tree.py
cd hardened
python -B test_guard.py
python -B -m unittest -v test_lifecycle
bash -n frame-stream-audio-fix reproduce_upstream_filter.sh
bash reproduce_upstream_filter.sh
shellcheck frame-stream-audio-fix reproduce_upstream_filter.sh
sha256sum -c SHA256SUMS
```

The original 34 scenarios are retained. One partial-demotion mock is corrected to model the real backend's uncertain D-Bus error; its expectation is strengthened to require retained IN_FLIGHT/no forced restore. The additional 45-case lifecycle suite covers exit/reuse proof, archive durability/failure/corruption, crash-window idempotency, uncertain RPCs, strict scheduler gates, dynamic priority-60 threads, baseline-three-plus-transient round trips, and Linux procfs edge cases. Scheduler, RTKit and service operations are fake. Child termination and local temporary filesystem operations are real. Raw device fixtures remain private; CI uses synthetic equivalents.

GitHub Actions runs Python tests on Ubuntu/Windows and Bash syntax/reproduction/ShellCheck on Ubuntu, plus public-tree/hash checks. Actions are SHA-pinned with read-only repository permissions. No SSH, device secrets or deployment credentials. Generated test reports are not committed. Read CI results for the exact candidate commit.

## Residual risks

- Short real-device RTKit/systemd scheduler/lifecycle acceptance and normal-boot autostart passed on the tested device. Long-duration VRChat/SteamVR/PCVR behavior, crackle under load, repeated reconnects and standalone audio still need user testing; this does not certify every device or workload.
- Numeric PID/TID APIs cannot atomically accept expected start-time identity. Pre/post checks reduce but cannot eliminate the final reuse/exec race.
- Tick-resolution start times are not cryptographic identity. Same-tick reuse and unusual procfs namespaces/visibility are residual limitations; ambiguous observed changes block. Do not run inside an alternate PID namespace/procfs or race other scheduling tools.
- Retirement archives grow with ended tracked lifetimes; there is no automatic pruning. Disk/fsync errors block safely and may require operator review.
- IN_FLIGHT crashes fail closed, not necessarily auto-recover. Never erase unresolved state or force uninstall. Explicit old-boot archival acknowledgment is not successful restoration.
- rename/fsync assumes a working supported filesystem. Checksums detect accidental corruption, not deliberate same-user tampering.
- Hard kill during uninstall may require manual non-overwriting recovery from the retained archive.
- Non-realtime audio may crackle under load. Status output is advisory, not an atomic global snapshot or proof of restoration.

No personal inventories, LAN addresses, SSH keys, device logs, local checkout paths or credentials are included. See [publication scope](docs/PUBLICATION.md).
