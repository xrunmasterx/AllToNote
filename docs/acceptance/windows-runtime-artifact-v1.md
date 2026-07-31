# Windows Runtime directory candidate v1

```yaml
status: candidate-pass
public_release: false
platform: windows-x86_64
runtime_source_commit: ac1fca6b0d9149bbcaa5431c8b007ee1a7b0f368
verified_at: 2026-07-31
```

## Artifact

- Local candidate: `G:\.alltonote-release\runtime-portable-sqlite-v5`
- Payload files covered by the internal SHA-256 manifest: 694
- `release/file-manifest.json` SHA-256: `af98a6827a86316c30c94818fbf95cc00e198ed364bd9c2f0a1fdf98499d50b9`
- All 694 listed files were reread after publication with no missing, extra, byte-length, or SHA-256 mismatch; the only additional physical file is the manifest itself.
- No worktree, release-input, or builder-Python absolute path was found in the payload. `direct_url.json`, reparse points, bytecode caches, and generated pip console launchers are absent.
- A copied candidate under `G:\.alltonote-release\Runtime 候选 有空格 v5 relocation` passed `version` and `runtime info` after relocation; the copy was removed after the smoke.
- This is a directory candidate with a relative `alltonote.cmd` launcher and owned `python.exe`; it is not an installer or a signed public artifact.

## Locked binary identity

| Component | Identity | Release input integrity |
| --- | --- | --- |
| CPython | 3.14.6, CPython, AMD64, 64-bit | official `python-3.14.6-embed-amd64.zip`, SHA-256 `df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c` |
| SQLite | 3.53.4 | official `sqlite-dll-win-x64-3530400.zip`, SHA3-256 `deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a` |
| SQLite source | `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc` | exact Runtime probe and child-process WAL Gate identity |
| Runtime wheels | 15 exact wheels | filename, byte length, and SHA-256 frozen in `backend/runtime-windows-x86_64.lock.json` |

The assembler disables pip configuration and environment overrides, installs all 15 verified wheel files by absolute path with `--isolated --no-index --no-deps`, and does not permit dependency resolution or another package source. Pip-generated local-source metadata and console launchers are removed from private staging together with their exact `RECORD` rows before the Runtime is probed or published.

The release probe enumerated the modules loaded by the candidate process and bound them to artifact-relative paths:

- executable: `python.exe`
- Python library: `python314.dll`
- SQLite extension: `_sqlite3.pyd`
- SQLite library: `sqlite3.dll`
- loaded `sqlite3.dll` SHA-256: `ab57d0437795ecc757cb693f32ea224173fa9856594d95cfa6b5033e645cd1ec`

No system Python or system SQLite fallback was accepted by the probe.

## Candidate smoke

The offline release assembler ran these commands from the assembled directory before publishing it:

1. `version --json`
2. `runtime info --json`
3. `runtime doctor --json`
4. `workspace init` using a Chinese name and a path containing Chinese characters and spaces
5. `runtime sqlite-wal-gate` using an isolated machine-state and scratch root

The SQLite Gate passed all 1/4/8/16-connection scenarios: short writes, mixed reads/writes, a live PASSIVE-checkpoint overlap handshake, forced busy followed by caller-controlled retry, portable-commit writer-lock timing, uncommitted and acknowledged crash recovery, final TRUNCATE checkpoint, `integrity_check`, foreign-key check, WAL mode, and schema version. The result deliberately retained `parallel_job_execution_enabled=false`; this artifact proves the binary boundary and does not itself enable parallel production.

## Source regression

- Final focused Runtime/SQLite/release-tool suite: 47 passed.
- The complete backend test collection was covered in non-overlapping partitions on the final affected code: 2295 passed, 2 skipped, 3 pre-existing warnings, and 3 subtests passed.
- The candidate manifest was independently reread and every listed byte length and SHA-256 was verified after publication.

## Open release gates

This candidate must not be called stable or distributed to ordinary users until all of the following are complete:

- run the same artifact under a fresh non-admin Windows user/VM with a Chinese-and-space profile, Defender enabled, no repository, no developer Python, and no pre-existing AllToNote machine state;
- replace or wrap `alltonote.cmd` with the selected signed stable launcher and per-user installer/discovery/PATH contract;
- pin and attest the builder Python/pip toolchain, independently reconcile installed payload files against the locked wheel `RECORD` data plus the documented metadata-removal policy, and sign that provenance;
- add Authenticode/timestamp, signed outer manifest/archive, and complete Runtime SBOM/license aggregation;
- prove update, rollback, repair, and uninstall while the test Vault hash stays unchanged;
- run Video and Document real-input E2E from this exact Runtime plus their signed Packs, including restart/recovery and clean-user Pack discovery;
- independently review the release assembler and repeat the full backend regression after its final diff.
