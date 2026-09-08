# Changelog

All notable changes to symbolgraph are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [SemVer](https://semver.org/).

## [Unreleased]

### Fixed

- **`sg init` left agents with tools they never called.** It registered the MCP
  server but wrote no instruction file for Claude Code — and the one instruction
  file it did write, `AGENTS.md`, is not a file Claude Code reads. Every editor
  now gets the guidance block in the file it actually loads (`CLAUDE.md`,
  `GEMINI.md`, `AGENTS.md`, `.github/copilot-instructions.md`), and the block
  itself names the MCP tools and says to prefer them over grep.
- **Codex registration silently skipped every project after the first.**
  `~/.codex/config.toml` is global and the "already configured" marker was the
  project-agnostic string `sg-mcp`, so the second repo you ran `sg init` in was
  a no-op. The marker is now this project's own `[mcp_servers.sg-<slug>]` table.
- **VS Code never saw the server** — its `mcp.json` keys servers under
  `servers`, not `mcpServers`, and wants an explicit `"type": "stdio"`.
- **OpenCode never saw the server** — its entry takes an argv list
  (`"command": ["sg-mcp"]`) plus `"type": "local"`, not a bare string.
- **Bumping the instruction-block version appended a second copy** instead of
  upgrading the existing one; blocks of any version are now replaced in place.
- **`sg uninstall` left the instruction blocks and the Codex table behind.** It
  now removes everything `sg init` wrote, preserving any of your own content in
  a shared file and deleting a file that only ever held our block.
- **`sg doctor` exited 1 on a healthy setup.** Having no embedding backend is a
  supported configuration (the line even said `(ok)`), as is a non-empty
  embedding queue when nothing can drain it.
- `sg init` no longer swallows git-hook installation failures; it reports them.
- **Codex config resolution now honors `CODEX_HOME`**, as codex itself does, so
  we write where codex reads. `~` expansion consults `HOME` on POSIX but
  `USERPROFILE` on Windows, which made the location unpredictable there — and
  made the test suite write into the real home directory on Windows CI. The
  suite now redirects `CODEX_HOME` session-wide, so no test can reach a
  developer's real `~/.codex/config.toml`.

### Added

- `sg doctor` checks both halves of the wiring: `mcp registered` and `agent
  instructions`. A registered server the agent was never told about produces
  zero tool calls and otherwise looks identical to broken tools.
- `sg init` detects Claude Code, Gemini and Copilot projects, and prints the
  next steps (index, restart the editor, verify with `sg doctor`).

### Changed

- The MCP server's own `instructions` string now lists each tool and states the
  "use these instead of grep/glob/file-reads" rule.

- Packaging/publish readiness.
- Removed the external benchmark data (`benchmarks/*_queries.json`,
  `benchmarks/results/`) and its attribution file — those query sets
  were derived from a third-party project's benchmark suite, and this
  project no longer ships anything derived from another project's work.
  A real-repo benchmark with original queries has since been run with
  pre-registered queries — see `benchmarks/results/SUMMARY.md`.

## [0.1.0] — 2026-09-04

First tagged release. Seeded from `feat(P*)` commit history (224 commits
total); grouped by area rather than listed commit-by-commit.

### Added

- **Symbol graph** — tree-sitter based multi-language extraction (Python,
  JS/TS/TSX/JSX, Go, Rust, Java, C#, C, C++), stable-key symbols with
  `CALLS` / `EXTENDS` / `IMPLEMENTS` / `HAS_TYPE` / `RETURNS` /
  `DECLARES` relationships and cross-file import/export resolution,
  including `export * from` / `export {x} from` re-exports and
  `property_signature` / `method_signature` interface members.
- **Hybrid retrieval** — FTS5 (per-column BM25 weights) + sqlite-vec
  cosine (numpy fallback where the extension can't load) + exact-symbol +
  graph expansion, fused by RRF and a learned/tuned reranker with a
  per-file cap and token budget.
- **Incremental indexing** — Merkle-hashed change detection, append-only
  chunk reuse via `content_hash`, interface-aware re-resolution instead
  of full snapshot rewrites, embedding-dimension migration on model change.
- **CLI** — `sg init` (8-editor matrix: Claude, Cursor, VS Code, OpenCode,
  Gemini, Copilot, Pi, Codex, plus `--agent all`), `index`, `status`,
  `search`, `definition`, `callers`, `callees`, `imports`, `context`,
  `embed`, `recall`, `timeline`, `export`, `prune`, `doctor`, `eval` /
  `eval-ab`, `uninstall`.
- **MCP server** (`sg-mcp`) — exposes the same tool surface to agents
  over the Model Context Protocol.
- **Ops** — resource governor (PSI/ONNX-thread caps, idle tracker,
  memory-pressure backoff), file locking for concurrent `sg index`, git
  hooks (`post-commit`/`post-checkout`/`post-merge`) for keep-fresh
  reindexing, a local FastAPI dashboard (HMAC bearer auth + CSRF checks,
  8 endpoints) with a coverage/savings view.
- **Security** — secret redaction (15+ regexes incl. Luhn-validated card
  numbers, `GENERIC_CREDENTIAL` heuristic) and PII scrubbing applied
  before anything is indexed or stored in session memory.
- **Evaluation** — fixed-benchmark suite (`sg eval`), paired A/B harness
  (`sg eval-ab`) against real coding-agent runs, and a reusable
  external file-level benchmark harness (`benchmarks/run_external.py`)
  for running against any repo with a `{query, expected_files}` set.
- **Session memory** — local, project-scoped decision/code-area/timeline
  recall, redacted the same way as the index.

### Packaging (this release)

- `LICENSE` (MIT).
- `ruff` added as a dev dependency and the codebase's first lint pass
  cleaned (116 findings on first run — this project had never been
  linted before).
- `hypothesis` and `httpx` added as dev dependencies — both were
  previously only pip-installed by hand, so their gated tests had been
  silently skipping in every clean environment, including CI.
- `mcp` capped `<3`, `tree-sitter` capped `<0.26` — both were unbounded
  and `uv tool install` ignores `uv.lock`; the tree-sitter cap fixed a
  live ABI mismatch this exact repo had already resolved into.
- PyPI metadata (`authors`, `keywords`, `classifiers`, `project.urls`)
  and a trusted-publishing (OIDC) `publish.yml`.

[Unreleased]: https://github.com/Deepjyoti-Sarmah/coding-RAG-system/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Deepjyoti-Sarmah/coding-RAG-system/releases/tag/v0.1.0
