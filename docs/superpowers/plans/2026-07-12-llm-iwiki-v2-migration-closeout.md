# llm-iwiki V2 Migration Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing uncommitted V2 migration in `E:\Agent_Learning\llm-iwiki` into a reviewed, tested, committed Phase 0 baseline without losing unrelated user work.

**Architecture:** This is a closeout plan, not a second migration. It treats the existing `codex/llm-wiki-v2-migration` worktree as the only source of the in-progress migration, inventories every changed path, verifies the V2 contract with the repository's real unittest discovery command, and commits the migration in reviewable groups before any new manifest or CLI work starts.

**Tech Stack:** Git, PowerShell, Python 3.11, stdlib `unittest`, Markdown.

## Global Constraints

- Work only in `E:\Agent_Learning\llm-iwiki` on branch `codex/llm-wiki-v2-migration`.
- Do not create a worktree from `HEAD` for Phase 0: the migration exists only as uncommitted changes in the current worktree.
- Do not use `git reset --hard`, `git checkout --`, `git clean`, or blanket deletion commands.
- Markdown under `raw/` and `wiki/` is durable data; `.cache/`, QMD databases, graph output, staging reports, and task state are rebuildable.
- The canonical V2 roots are exactly `raw/common`, `raw/personal`, `wiki/common`, and `wiki/personal`.
- Personal roots remain gitignored and must never be staged.
- Existing document bodies are not bulk-rewritten during closeout.
- Phase 1 must not start until the final status and test gates in this plan pass.

---

## File Responsibility Map

- Existing plan: `docs/superpowers/plans/2026-07-05-llm-wiki-v2-migration.md` — original structural migration instructions; preserve as historical implementation evidence.
- Create: `docs/superpowers/reports/2026-07-12-v2-migration-closeout.md` — immutable closeout inventory, test evidence, and explicit exclusions.
- Existing runtime/tool changes: `tools/*.py`, `tools/engine_kb_workflow/*.py` — V2 reader/writer/runtime implementation already under migration.
- Existing tests: `tests/test_v2_layout_contracts.py`, `tests/test_codex_alignment.py`, `tests/test_lint_contracts.py`, `tests/test_qmd_integration.py`, `tests/test_engine_kb_workflow.py` — Phase 0 acceptance suite.
- Existing rules/docs: `README.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.claude/commands/*.md`, `docs/wiki-schema.md`, `docs/wiki-architecture-v2.md`, `docs/llm-wiki-principles.md` — human contract.
- Migrated data: `raw/common/**`, `wiki/common/**`, deletions under legacy `raw/<owner>/**`, `wiki/topics/**`, `wiki/sources/**`, and generated/staging trees — data move to review separately from runtime code.
- Explicitly exclude unless separately justified by the user: `tasks.md`, `skill-definition-analysis.md`, and `rename_map.json` — root-level scratch artifacts are not named by the V2 design contract.

## Task 1: Freeze and Prove the Current Baseline

**Files:**

- Create: `docs/superpowers/reports/2026-07-12-v2-migration-closeout.md`
- Read: `docs/superpowers/plans/2026-07-05-llm-wiki-v2-migration.md`

**Interfaces:**

- Consumes: the current dirty worktree and the existing migration plan.
- Produces: a path/count/test inventory that later closeout tasks use as their review baseline.

- [ ] **Step 1: Verify branch and preserve the raw status evidence**

Run:

```powershell
git branch --show-current
git status --porcelain=v1 | Set-Content -Encoding UTF8 .cache\v2-closeout-status.txt
@((Get-Content .cache\v2-closeout-status.txt)).Count
```

Expected: branch is `codex/llm-wiki-v2-migration`; the captured baseline contains 1,563 entries at the time this plan was written. If the count has changed, continue only after explaining the delta in the closeout report; do not force the count back to 1,563.

- [ ] **Step 2: Run the actual repository test discovery command**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: `Ran 119 tests` and `OK` for the current baseline. Do not use `python -m unittest tests.test_*`; `tests/` is not a Python package and that command produces import errors.

- [ ] **Step 3: Create the closeout report with measured evidence**

Create `docs/superpowers/reports/2026-07-12-v2-migration-closeout.md` with this complete structure and replace only the command-produced counts/hashes when they differ:

```markdown
# LLM Wiki V2 Migration Closeout

- Branch: `codex/llm-wiki-v2-migration`
- Baseline commit: `72171c0`
- Baseline dirty entries observed during planning: `1563`
- Acceptance command: `python -m unittest discover -s tests -p "test_*.py" -v`
- Acceptance result observed during planning: `119 tests, OK`

## Canonical V2 Contract

- Shared raw input: `raw/common/`
- Private raw input: `raw/personal/` and gitignored
- Shared compiled knowledge: `wiki/common/`
- Private compiled knowledge: `wiki/personal/` and gitignored
- Durable source of truth: Markdown and adjacent evidence
- Rebuildable products: `.cache/`, QMD database, graph output, staging reports

## Commit Groups

1. Baseline closeout report.
2. Runtime and contract tests.
3. Architecture docs, agent rules, and command help.
4. Canonical raw/wiki data migration and deletion of tracked generated artifacts.

## Explicit Exclusions

- `tasks.md`
- `skill-definition-analysis.md`
- `rename_map.json`
- `raw/personal/**`
- `wiki/personal/**`
- `.cache/**`

These paths are not part of the closeout commit unless the user separately identifies them as intentional migration artifacts.

## Verification

The closeout is accepted only when the full unittest discovery suite passes, legacy visible roots are absent from the committed tree, personal roots are untracked, and `git status --short` contains only explicitly excluded user files.
```

- [ ] **Step 4: Verify the report contains no unresolved markers**

Run:

```powershell
$matches = Select-String -LiteralPath docs\superpowers\reports\2026-07-12-v2-migration-closeout.md -Pattern 'T[O]DO|T[B]D|PLACE[H]OLDER|待[定]'
if ($matches) { $matches; exit 1 }
```

Expected: exit 0 with no output.

- [ ] **Step 5: Commit the reviewed baseline report**

Run:

```powershell
git add -- docs/superpowers/reports/2026-07-12-v2-migration-closeout.md
git diff --cached --check
git commit -m "docs: record v2 migration baseline"
```

Expected: one documentation-only commit containing exactly the closeout report. This gives Task 1 an independently reviewable commit and prevents its evidence from being mixed into later runtime or data commits.

## Task 2: Gate the Migrated Tree Against Data Loss and Leakage

**Files:**

- Modify if required: `.gitignore`
- Verify: staged Git index and canonical worktree roots

**Interfaces:**

- Consumes: canonical roots from `tools/wiki_runtime.py`.
- Produces: staged-index proof that common content is present, personal content is excluded, and removed legacy files are either migrated or rebuildable.

- [ ] **Step 1: Verify ignore rules cover private and derived roots**

Ensure `.gitignore` contains these exact rules:

```gitignore
/raw/personal/
/wiki/personal/
/.cache/
```

Do not ignore `raw/common/`, `wiki/common/`, or `.llm-wiki/`.

- [ ] **Step 2: Stage only the canonical tree transition for the index gate**

Run:

```powershell
git add -A -- raw wiki graph docs/*/.doc-gen-staging docs/specs/_artifacts
git status --short
```

Expected: canonical `raw/common/**` and `wiki/common/**` additions plus corresponding legacy/generated deletions are staged; `raw/personal/**`, `wiki/personal/**`, `.cache/**`, `tasks.md`, `skill-definition-analysis.md`, and `rename_map.json` are not staged.

- [ ] **Step 3: Assert the staged index contains no private/cache/legacy paths**

Run:

```powershell
$tracked = @(git ls-files)
$forbidden = @($tracked | Where-Object { $_ -match '^(raw/personal/|wiki/personal/|\.cache/|wiki/(topics|sources|concepts|_generated)/)' })
if ($forbidden) { $forbidden; exit 1 }
$commonRaw = @($tracked | Where-Object { $_ -like 'raw/common/*' }).Count
$commonWiki = @($tracked | Where-Object { $_ -like 'wiki/common/*' }).Count
if ($commonRaw -eq 0 -or $commonWiki -eq 0) { exit 1 }
"tracked raw/common=$commonRaw; tracked wiki/common=$commonWiki; forbidden=0"
```

Expected: both canonical common counts are greater than zero and `forbidden=0`. `git ls-files` observes the staged index, so this checks the intended committed tree rather than only the old `HEAD`.

- [ ] **Step 4: Review deletions and renames before any commit**

Run:

```powershell
git diff --cached --summary --find-renames
git diff --cached --name-status | Group-Object { ($_ -split "`t")[0] } | Select-Object Name,Count
```

Expected: legacy topic/source paths become canonical common paths or disappear because they are generated `.doc-gen-staging`, graph output, or old navigation artifacts. Any deletion of unique source Markdown without a canonical replacement is a hard stop and must be restored or explicitly approved by the user.

## Task 3: Commit Runtime and Contract Tests

**Files:**

- Modify: `tools/wiki_runtime.py`
- Modify: `tools/query.py`
- Modify: `tools/qmd_runtime.py`
- Modify: `tools/qmd_inject_context.py`
- Modify: `tools/qmd.py`
- Modify: `tools/build_graph.py`
- Modify: `tools/lint.py`
- Modify: `tools/ingest.py`
- Modify: `tools/migrate_naming.py`
- Modify: `tools/engine_kb_workflow/orchestrator.py`
- Modify: `tests/test_v2_layout_contracts.py`
- Modify: `tests/test_codex_alignment.py`
- Modify: `tests/test_lint_contracts.py`
- Modify: `tests/test_qmd_integration.py`
- Modify: `tests/test_engine_kb_workflow.py`

**Interfaces:**

- Consumes: V2 path contract in `docs/wiki-architecture-v2.md`.
- Produces: tested V2 readers/writers while preserving legacy names only as explicit diagnostics.

- [ ] **Step 1: Unstage the data group without changing files**

Run:

```powershell
git restore --staged -- raw wiki graph docs
```

Expected: files remain present in the worktree; only the index is cleared for those roots.

- [ ] **Step 2: Stage the runtime and tests explicitly**

Run:

```powershell
git add -- tools tests
git diff --cached --check
git diff --cached --stat
```

Expected: only `tools/**` and `tests/**` are staged, and `git diff --cached --check` exits 0.

- [ ] **Step 3: Run the full suite on the staged runtime**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: all tests pass; the planning baseline was 119 tests.

- [ ] **Step 4: Commit the runtime migration**

Run:

```powershell
git commit -m "refactor: migrate wiki runtime to v2 layout"
```

Expected: one commit containing only runtime/tool and test files.

## Task 4: Commit the Human Contract and Command Surface

**Files:**

- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `GEMINI.md`
- Modify: `.claude/commands/wiki-graph.md`
- Modify: `.claude/commands/wiki-ingest.md`
- Modify: `.claude/commands/wiki-lint.md`
- Modify: `.claude/commands/wiki-query.md`
- Modify: `docs/wiki-schema.md`
- Create: `docs/wiki-architecture-v2.md`
- Create: `docs/llm-wiki-principles.md`
- Create: `docs/architecture-snapshot.md`
- Create: `docs/skill-specs/**`
- Create: `docs/specs/archive/**`
- Create: `docs/superpowers/plans/2026-07-05-llm-wiki-v2-migration.md`
- Create: `docs/superpowers/reports/2026-07-12-v2-migration-closeout.md`

**Interfaces:**

- Consumes: the committed runtime semantics from Task 3.
- Produces: one consistent human-readable contract with no active V1 instructions.

- [ ] **Step 1: Scan active docs for stale V1 instructions**

Run:

```powershell
rg -n "wiki/(topics|sources|concepts|_generated/atoms)|raw/(CFH|DFM|NZM)/" README.md AGENTS.md CLAUDE.md GEMINI.md .claude/commands docs/wiki-schema.md docs/wiki-architecture-v2.md docs/llm-wiki-principles.md
```

Expected: matches exist only in migration history, explicit `LEGACY_*` explanation, or “must not create” rules. Active commands must point at `raw/common` and `wiki/common`.

- [ ] **Step 2: Stage only contract documentation**

Run:

```powershell
git add -- README.md AGENTS.md CLAUDE.md GEMINI.md .claude/commands docs/wiki-schema.md docs/wiki-architecture-v2.md docs/llm-wiki-principles.md docs/architecture-snapshot.md docs/skill-specs docs/specs/archive docs/superpowers
git diff --cached --check
```

Expected: no raw/wiki data and none of the root scratch artifacts are staged.

- [ ] **Step 3: Commit the contract documentation**

Run:

```powershell
git commit -m "docs: define llm wiki v2 contract"
```

Expected: one documentation-only commit.

## Task 5: Commit the Canonical Data Migration

**Files:**

- Create/rename: `raw/common/**`
- Create/rename: `wiki/common/**`
- Delete: tracked legacy `raw/<owner>/**`, `wiki/topics/**`, `wiki/sources/**`, `wiki/concepts/**`, `wiki/_generated/**`
- Delete: tracked `.doc-gen-staging/**` and graph build products
- Modify: `.gitignore`
- Modify: `wiki/index.md`

**Interfaces:**

- Consumes: the V2 runtime and human contract from Tasks 3–4.
- Produces: a committed repository whose visible durable data matches the V2 roots.

- [ ] **Step 1: Stage the intended data transition**

Run:

```powershell
git add -A -- raw wiki graph docs/*/.doc-gen-staging docs/specs/_artifacts .gitignore
git status --short
```

Expected: root scratch artifacts remain untracked; private and cache roots remain absent.

- [ ] **Step 2: Prove common content exists and private content is not staged**

Run:

```powershell
$commonRaw = @(git diff --cached --name-only --diff-filter=ACMR | Where-Object { $_ -like 'raw/common/*' }).Count
$commonWiki = @(git diff --cached --name-only --diff-filter=ACMR | Where-Object { $_ -like 'wiki/common/*' }).Count
$private = @(git diff --cached --name-only | Where-Object { $_ -like 'raw/personal/*' -or $_ -like 'wiki/personal/*' -or $_ -like '.cache/*' }).Count
if ($commonRaw -eq 0 -or $commonWiki -eq 0 -or $private -ne 0) { exit 1 }
"raw/common additions=$commonRaw; wiki/common additions=$commonWiki; private=$private"
```

Expected: both common counts are greater than zero and `private=0`.

- [ ] **Step 3: Run the full contract suite against the staged tree**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tools/lint.py
```

Expected: unittest suite passes. Lint must return its documented clean result; warnings explicitly classified as deferred legacy body-content warnings are allowed only when recorded in the closeout report.

- [ ] **Step 4: Commit the data migration**

Run:

```powershell
git commit -m "data: move wiki content to v2 roots"
```

Expected: one data migration commit with rename detection visible in `git show --summary --find-renames HEAD`.

## Task 6: Final Phase 0 Gate

**Files:**

- Verify: entire repository
- Modify only if test evidence changed: `docs/superpowers/reports/2026-07-12-v2-migration-closeout.md`

**Interfaces:**

- Consumes: all Phase 0 commits.
- Produces: the clean commit hash that Phase 1 uses as its base.

- [ ] **Step 1: Run fresh verification**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tools/lint.py
git diff --check HEAD~4..HEAD
```

Expected: all tests pass, lint is clean under the documented policy, and diff check exits 0.

- [ ] **Step 2: Verify the committed tree boundary**

Run:

```powershell
$tracked = @(git ls-files)
$forbidden = @($tracked | Where-Object { $_ -match '^(raw/personal/|wiki/personal/|\.cache/|wiki/(topics|sources|concepts|_generated)/)' })
if ($forbidden) { $forbidden; exit 1 }
git ls-tree -d --name-only HEAD raw wiki
```

Expected: no forbidden tracked paths; canonical common roots are present.

- [ ] **Step 3: Verify only explicit exclusions remain dirty**

Run:

```powershell
git status --short
```

Expected: clean, or only the explicitly excluded root scratch artifacts `tasks.md`, `skill-definition-analysis.md`, and `rename_map.json`. Any modified runtime, test, contract, raw, or wiki file means Phase 0 is not closed.

- [ ] **Step 4: Record the Phase 0 base commit**

Run:

```powershell
git rev-parse HEAD
git log -3 --oneline
```

Expected: four reviewable commits for the baseline report, runtime/tests, docs/rules, and canonical data. Use this exact final hash as the base when creating the Phase 1 worktree.
