# Publication and branch policy

- Repository target: https://github.com/Haritan4141/Frame-Stream-Audio-Fix
- Upstream: https://github.com/Proxian/Frame-Stream-Audio-Fix
- Baseline: `69669be81ee3571125ef3ff8a771523b5138c13b`
- Keep upstream ancestry intact. `main` is upstream-only; review updates before fast-forwarding it. No automatic upstream sync.
- `hardened` contains the separately reviewed candidate. Do not install merely because this branch moved. Review each new revision and pin a full commit SHA outside the moving branch.
- On 2026-09-27, `hardened` was fast-forwarded from `141f7a2ddd4a93f1b2030077e7a6a59ec4dae125` to the deployed and short-accepted `69f8e9dcd9199f881fb40bb37178635f559f8ce9`, after rechecking ancestry and exact-commit successful Ubuntu/Windows/ShellCheck CI. `main` remains `69669be81ee3571125ef3ff8a771523b5138c13b`; default branch remains `hardened`. No force push or history rewrite.
- `hardened-transient-lifecycle` retains the same runtime commit as history. `hardened-docs-boot-autostart` carries only later documentation; keep its documentation commit distinct from the deployed runtime commit. Neither moving ref is automatic deployment authority.
- Deployed commit at initial publication: **NONE / NOT_INSTALLED**. Repository publication is not headset deployment.
- Current deployment: runtime `69f8e9dcd9199f881fb40bb37178635f559f8ce9`, short acceptance passed, enabled/active, Boot Autostart VERIFIED / PASS; long-duration user test IN PROGRESS / NOT YET PASS. See the README for verification scope. Previous blocked evidence remains private and is not erased by promotion.
- Preserve MIT LICENSE/copyright. The original root script is comparison evidence, not the hardened installer.
- Publication includes an explicit allowlist of runtime, tests, workflow and generic docs. Excludes private audit logs, actual boot IDs, network identity, SSH materials, credentials, generated logs, user-specific local paths and private management files.
- Generic runtime paths (`~/.local/bin`, `~/.config/systemd/user`, `~/.local/state`), the product-default frame/steamos identity gate and synthetic test values are functional documentation, not private host inventory.
- Automated checks are a bounded heuristic, not proof that every possible secret format is detectable. Review the staged tree/diff and author metadata before each push.
- Actions use pinned action SHAs, read-only permissions and fake runtime backends, with no device access or deploy step.
- Human-managed installation records must distinguish upstream baseline, reviewed candidate SHA, deployed SHA (none until installed), runtime hashes, tests and their scope.
