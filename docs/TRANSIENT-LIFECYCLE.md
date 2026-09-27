# Transient audio thread lifecycle — design and review

Base: `141f7a2ddd4a93f1b2030077e7a6a59ec4dae125`. Upstream baseline remains `69669be81ee3571125ef3ff8a771523b5138c13b`; original script and MIT notices are unchanged.

The original LOCAL-only development phase performed no device access or deployment and preserved the then-blocked device evidence. Subsequent separately approved migration and short acceptance of runtime `69f8e9dcd9199f881fb40bb37178635f559f8ce9` completed on 2026-09-27, followed by read-only normal-boot autostart and retrospective sleep/resume checks. See [current status and limits](../README.md#status-and-limits). This document retains the design/local-test rationale; those local tests alone are not real-device or long-session audio acceptance.

## Why not a three-thread allowlist

A short device trial encountered a new priority-60 RR|RESET audio-filter thread while streaming. It was demoted, then exited; the baseline guard retained its TRACKED record and blocked. All baseline audio workers were independently back at their original scheduler values, but this was not a complete round-trip pass. Dynamic RR threads remain eligible; no name/PID/baseline-three allowlist is added.

## Decision table

| Durable phase / observation | Action |
| --- | --- |
| TRACKED, identical live thread | Existing guarded restoration plus independent getters; clear only verified original values. |
| TRACKED, same boot, confirmed task absence | Durable `RETIRED_EXITED_TRACKED_THREAD` archive, then active removal. NOT restored. |
| TRACKED, later process/thread start ticks, same numeric IDs/boot | Archive `RETIRED_PID_REUSED_TRACKED_THREAD` or `RETIRED_TID_REUSED_TRACKED_THREAD`; never restore the replacement with the old record. |
| Same start ticks but changed UID/exe/name, unexpected scheduler | NO_TOUCH / BLOCK. Not lifetime proof. |
| DEMOTE_IN_FLIGHT / RESTORE_IN_FLIGHT, including missing/reused IDs | BLOCK before any retirement lookup/probe. Pending or insufficiently verified RPC is not assumed complete. |
| WRITE_AHEAD, confirmed exit/reuse | Archive a distinct WRITE_AHEAD event: no D-Bus dispatch can precede durable IN_FLIGHT. |
| WRITE_AHEAD, live but scheduler differs from saved original | BLOCK, no mutation: this guard has not dispatched. |
| Different boot, lower ticks, inconsistent reads, unknown identity | BLOCK; no new implicit old-boot bypass. |
| PermissionError, EIO, malformed reads, general OSError | BLOCK; never reinterpret as exit. |

Only LinuxBackend's positive lifetime proof qualifies. Missing task stat must be corroborated by absence of BOTH `/proc/PID/task/TID` and direct `/proc/TID`. Two identical observations and unchanged current boot are required. The direct check matters because a terminated group leader can make `/proc/PID/task` unavailable while another thread lives. This is documented by the primary [Linux proc_pid_task manual](https://www.man7.org/linux/man-pages/man5/proc_pid_task.5.html) (checked 2026-09-27). A direct TID that remains visible blocks instead of assuming death. ESRCH from a getter is not enough on its own; it triggers the same corroborating probe. Unknown proc-stat errors themselves remain blocked.

For visible reuse, two consistent strictly later process start ticks, or strictly later thread start ticks under the same process start, prove the saved lifetime is over. Lower/equal ambiguous ticks are never reuse proof. Live identity comparison still includes all prior fields. Fresh discovery can separately track an eligible new identity; the retired record is never applied to it.

## v1 compatibility and RPC ordering

The active JSON schema stays version 1 with unchanged record keys and checksum encoding. Existing state is read directly without migration or weakening validation.

The baseline wrote TRACKED immediately after an acknowledged RPC, before the getter check. Therefore legacy TRACKED alone does NOT prove demotion verification. It does signify a known-completed request (or a pre-dispatch/synchronous backend failure), not a possibly delayed D-Bus request. Termination removes that old lifetime's scheduler state regardless of whether demotion had taken effect; archive events explicitly do not claim successful restoration/demotion. The privately preserved device record additionally has a verified-demotion journal event.

New RPC paths retain IN_FLIGHT until the post-RPC getter verifies expected identity and scheduler values. If the task exits after acknowledgment but before verification, they conservatively block. Nonzero/timeout D-Bus replies stay uncertain. A crash before persisting IN_FLIGHT leaves WRITE_AHEAD and necessarily no dispatch; a crash after persistence, even before actual dispatch, stays conservative IN_FLIGHT. No new schema phase or unsafe automatic RPC retry is introduced.

Restore starts only from the exact original state or exact OTHER|RESET/0/nice-11 demotion. Half-looking states such as OTHER/nice0 or RR/nice-11 in TRACKED are not attributed to this guard and are blocked. To avoid creating an ambiguous TRACKED intermediate, restore requests RR first, verifies RR/original priority/nice-11, then resets nice and verifies the full original state while RESTORE_IN_FLIGHT remains durable. A failure in that final stage stays blocked. This restore order subsequently passed the separately observed short real-device trial for the pinned runtime; local mocks alone do not establish RTKit integration success on any device.

## Durable audit protocol

Under the existing exclusive writer lock:

1. Validate/load active state and eligible phase.
2. Reuse an already committed archive for that exact complete record, or obtain affirmative lifetime proof.
3. Store private `retired/<SHA256(canonical complete record)>.json`: complete record, reason, UTC timestamp, prior full state-file SHA256, prior body checksum and two proof observations. The envelope has its own checksum/version.
4. Write via same-directory temporary file, file fsync, atomic replace, directory fsync. Read back/checksum/proof validate and re-fsync the archive and containing directory before active removal.
5. Atomically save active state excluding only that exact record; emit its distinct retirement event.

An archive failure leaves active state. Archive succeeded but state-save failed/crashed: the next run validates the existing archive, preserves its timestamp/bytes, re-seals durability, then removes the still-matching active record. No scheduler operation is needed for this idempotent retry. A crash after removal leaves the audit archive and empty/remaining valid state. Corrupt/conflicting archives or active-state corruption block without overwrite. Temp remnants from a hard kill are not evidence and are not auto-cleaned. There is no archive pruning.

Private Linux mode is 0700 for retirement directory and 0600 for files. Windows runs mocks only; directory fsync is a Linux runtime guarantee, not a Windows test claim. Checksums detect accidental corruption, not hostile same-user edits.

## Validation scope

- All 34 original scenarios remain. The partial-demote mock now raises RpcUncertain, matching the real backend for a nonzero/timeout D-Bus reply; its test is strengthened to demand retained IN_FLIGHT/no forced restore rather than assume cancellation. The other 33 scenarios are unchanged, including conservative stale/exit behavior for a backend without affirmative lifetime proof.
- 45 additional lifecycle tests cover every decision above, strict no-touch half-states, real child termination between archive and removal, partial writes/fsync failures, repeated retry, dynamic priority-60 workers, and a service-loop simulation of three stable workers plus one transient through stream end. Retirement of an earlier record must preserve later records' in-memory references so their RPC phases are still persisted; an explicit fault-injection test covers this ordering.
- CI has Ubuntu and Windows jobs for both suites, plus ShellCheck/Bash validation on Ubuntu and publication/hash/privacy gates on both.
- A PRIVATE management-host test uses the exact preserved v1 state bytes, asserts its existing SHA256 and source immutability, supplies mock task-exit evidence, and verifies archive1 / active0 / zero scheduler operations. Public tests use synthetic equivalent records; private boot IDs, raw device logs and local paths are not published.

Remaining limitations: numeric PID/TID operations have a final precheck-to-syscall/RPC race; start ticks have finite resolution; alternate procfs visibility/namespaces are unsupported; a task disappearing in other mutation windows may conservatively block. Disk/fsync failures or full archive storage block. No audio-cure claim follows from local tests or the subsequent short real-device acceptance. Existing device state must not be removed or recovered without separate approval.
