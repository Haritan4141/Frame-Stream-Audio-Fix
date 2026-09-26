# Publication and branch policy

- Repository target: https://github.com/Haritan4141/Frame-Stream-Audio-Fix
- Upstream: https://github.com/Proxian/Frame-Stream-Audio-Fix
- Baseline: `69669be81ee3571125ef3ff8a771523b5138c13b`
- Keep upstream ancestry intact. `main` is upstream-only; review updates before fast-forwarding it. No automatic upstream sync.
- `hardened` contains the separately reviewed candidate. Do not install merely because this branch moved. Review each new revision and pin a full commit SHA outside the moving branch.
- `hardened-transient-lifecycle` is a LOCAL-only successor candidate based on `141f7a2ddd4a93f1b2030077e7a6a59ec4dae125`. Keep the baseline commit and deployed runtime/state untouched. The new branch is not device deployment authority.
- Deployed commit at initial publication: **NONE / NOT_INSTALLED**. Repository publication is not headset deployment.
- Preserve MIT LICENSE/copyright. The original root script is comparison evidence, not the hardened installer.
- Publication includes an explicit allowlist of runtime, tests, workflow and generic docs. Excludes private audit logs, actual boot IDs, network identity, SSH materials, credentials, generated logs, user-specific local paths and private management files.
- Generic runtime paths (`~/.local/bin`, `~/.config/systemd/user`, `~/.local/state`), the product-default frame/steamos identity gate and synthetic test values are functional documentation, not private host inventory.
- Automated checks are a bounded heuristic, not proof that every possible secret format is detectable. Review the staged tree/diff and author metadata before each push.
- Actions use pinned action SHAs, read-only permissions and fake runtime backends, with no device access or deploy step.
- Human-managed installation records must distinguish upstream baseline, reviewed candidate SHA, deployed SHA (none until installed), runtime hashes, tests and their scope.
