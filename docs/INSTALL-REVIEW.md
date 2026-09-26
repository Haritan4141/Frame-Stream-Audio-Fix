# Future installation review — not automatic deployment

Only use this after separate approval for device installation and a short scheduler round-trip test. Upstream install/uninstall commands and moving branch downloads must not be used.

**Lifecycle development is LOCAL ONLY.** A previously deployed candidate and unresolved state must remain unchanged. This fresh-install procedure is NOT an upgrade procedure for that device. In-place replacement, recover/retire, stop/start/enable or any state/archive change needs a new explicit device-review approval and a separately reviewed non-overwriting migration plan.

## Pinning on the management host

Use a new empty checkout directory. Replace the placeholder with the full, independently recorded **40-character reviewed commit SHA**, never a branch name:

```sh
git clone --no-checkout https://github.com/Haritan4141/Frame-Stream-Audio-Fix.git frame-audio-reviewed
cd frame-audio-reviewed
git fetch origin <REVIEWED_40_CHARACTER_COMMIT_SHA>
git checkout --detach <REVIEWED_40_CHARACTER_COMMIT_SHA>
git rev-parse HEAD
git status --porcelain
cd hardened
sha256sum -c SHA256SUMS
```

Verify HEAD against the independently approved SHA, clean status, upstream ancestry and the reviewed diff. Do not discover the approved SHA by resolving the current branch. Transfer only the three runtime files, LICENSE and SHA256SUMS to a new, non-overwriting staging directory in the device user's HOME. Do not transfer credentials, private keys or test harnesses.

## Preconditions on the device

- Read-only identity check: hostname frame, user steamos, non-root UID.
- Active PCVR session ended; operator present for audio tests.
- Reread audio thread state and dependencies. Only supported policies/priorities/nice qualify. Do not install packages or increase permissions to bypass a failed check.
- Existing Fix/script/helper/unit/state: stop for review; never overwrite or discard unresolved state.
- Verify the staged bytes with `sha256sum -c SHA256SUMS`.
- No system/SSH/Developer Mode/Steam changes. Do not restart audio services.

## Exclusive user-area placement (approved future phase only)

From the verified staging directory, after checking all preconditions:

```sh
set -euo pipefail
test "$(hostname)" = frame
test "$(id -un)" = steamos
test "$(id -u)" != 0
sha256sum -c SHA256SUMS
/usr/bin/python3 -B - <<'PY'
from pathlib import Path
import os, shutil
import frame_audio_guard as g
os.umask(0o077)
g.safe_path(Path.cwd(), directory=True)
files = g.Files(Path.home())
state = Path.home() / '.local/state' / g.NAME
if state.exists() or state.is_symlink():
    raise SystemExit('Existing state: STOP; never erase')
pairs = [(Path('frame-stream-audio-fix'), files.bin, 0o700),
         (Path('frame_audio_guard.py'), files.helper, 0o600),
         (Path('frame-stream-audio-fix.service'), files.unit, 0o600)]
for src, dst, mode in pairs:
    g.safe_path(src.absolute())
    g.safe_path(dst.parent, directory=True, create=True)
    g.safe_path(dst)
    if dst.exists() or dst.is_symlink():
        raise SystemExit('Existing destination: STOP; no overwrite')
for src, dst, mode in pairs:
    with dst.open('xb') as out, src.open('rb') as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    dst.chmod(mode)
    g.sync_dir(dst.parent)
PY
```

If a partial copy fails, retain artifacts and inspect; never enable the service automatically. Recheck installed hashes/owners/modes; retain LICENSE in staging. Commands below are separate gates, not a blind success chain:

1. `systemd-analyze --user verify "$HOME/.config/systemd/user/frame-stream-audio-fix.service"`
2. `systemctl --user daemon-reload`
3. `bash "$HOME/.local/bin/frame-stream-audio-fix" recover` then `status`; require zero unresolved state.
4. `systemctl --user start frame-stream-audio-fix.service`; do not enable yet.
5. Verify active/running, expected user FragmentPath/owner, no-stream state zero and original audio thread states.
6. Operator starts a short stream. Eligible threads should be OTHER|RESET, priority0, nice-11; durable TRACKED entries retain their originals.
7. End stream. Independently measure original identity/policy/priority/nice restored and state zero. Check safe stop/recovery/inactive in the approved test window.
8. Only after these gates pass, enable the user unit and start if needed. Verify enabled/active/running.
9. User tests VRChat/SteamVR/long PCVR, heavy-load crackle, reconnect, post-stream restoration and new PipeWire SIGKILL events. Installation success is not audio-fix success.

## Transactional removal

For this hardened candidate only:

```sh
bash "$HOME/.local/bin/frame-stream-audio-fix" uninstall
```

It requests stop; takes the exclusive lock; restores/verifies all records; requires inactive and zero unresolved state; disables/rechecks; archives the three control files in user state; reloads; verifies not-found/inactive/empty FragmentPath. Ordinary exceptions restore moved files and reload again. Unresolved restoration yields UNINSTALL_BLOCKED_RESTORE_FAILED and retains files/state. A rollback reload failure is reported as UNINSTALL_FILES_RESTORED_BUT_ROLLBACK_RELOAD_FAILED. Never force completion by deleting files/state or restarting the OS.

Status is advisory. IN_FLIGHT, ambiguous identity, corrupt/missing state or a hard-killed uninstall require manual review. Confirmed ended TRACKED/WRITE_AHEAD lifetimes may be archived/retired by the new candidate, never reported as successfully restored. Preserve all retirement/uninstall archives. Do not restart audio services or the device without approval.

## Explicit old-boot acknowledgment

There is no same-boot IN_FLIGHT bypass. Only after a separately approved controlled reboot, verified different boot for every saved record, service inactive and independent current audio audit, an operator may approve archival acknowledgment (not restoration):

```sh
sha256sum "$HOME/.local/state/frame-stream-audio-fix/state.json"
bash "$HOME/.local/bin/frame-stream-audio-fix" acknowledge-previous-boot \
  --confirm-state-sha256 '<ACTUAL_LOWERCASE_STATE_SHA256>' \
  --acknowledge-not-restored
```

The exact JSON is archived before active state is cleared. Corrupt JSON/checksum prevents this command too. Never invent empty state or call old-boot acknowledgment a successful restore.
