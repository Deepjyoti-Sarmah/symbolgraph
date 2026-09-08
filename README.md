# symbolgraph

<p align="center">
  <strong>Your AI coding agent shouldn't have to read your whole repo to answer one question.</strong><br>
  A local-first code index that gives agents exact definitions and their relationships — not 50k tokens of file dumps.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-blue?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/tests-677%20passed-brightgreen?style=flat-square" alt="tests">
  <img src="https://img.shields.io/badge/coverage-80.68%25%20branch-brightgreen?style=flat-square" alt="coverage">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="license">
</p>

<p align="center">
  <sub>Python 3.11+ · macOS · Linux · Windows · Your code never leaves your machine</sub>
</p>

---

## What is this?

When you ask a coding agent *"where does login happen?"*, it usually greps, opens
half a dozen files, and burns thousands of tokens rebuilding context you already
have on disk.

symbolgraph reads your repository **once** and builds a real index of it: every
function, class and method, plus the typed relationships between them — what
calls what, what extends what, what returns what. Your agent then asks the index
a question and gets back *the definition it needed and the things connected to
it*, inside a token budget.

Three things make it different from plain text search:

- **It indexes symbols, not text spans.** A result is always a complete
  definition, never the middle of a function.
- **It knows relationships.** `CALLS`, `EXTENDS`, `IMPLEMENTS`, `HAS_TYPE`,
  `RETURNS` — resolved across files, so callers and callees come back with the
  answer instead of needing a second search.
- **It runs entirely on your machine.** One SQLite file inside your repo. No
  account, no upload, no telemetry.

---

## Install

Requires **Python 3.11+**. Install from
[PyPI](https://pypi.org/project/symbolgraph/):

```bash
uv tool install symbolgraph   # recommended
# or: pipx install symbolgraph
# or: pip install symbolgraph
```

From a checkout instead (for development):

```bash
git clone https://github.com/Deepjyoti-Sarmah/coding-RAG-system
cd coding-RAG-system
pip install -e .
```

This installs two commands: **`sg`** (the CLI) and **`sg-mcp`** (the MCP server
your agent talks to).

```bash
sg --version    # 0.1.0
```

---

## Quick start

Run these three commands inside any repository you want indexed.

**1. Build the index**

```bash
$ sg index .
Indexed into .sg/index.sqlite
  parsed files:        349
  resolved references: 36711
  new: 350
```

Everything lives in `.sg/index.sqlite` inside your project. Delete that folder
and it's gone — your source is never modified.

**2. Connect your agent**

```bash
$ sg init --agent all
Wrote .mcp.json
Wrote CLAUDE.md
Wrote .cursor/mcp.json
Wrote AGENTS.md
Wrote .vscode/mcp.json
Wrote .github/copilot-instructions.md
Wrote opencode.json
Wrote .gemini/settings.json
Wrote GEMINI.md
Wrote ~/.codex/config.toml

Next: run `sg index .`, then restart your editor so it picks
up the MCP server. `sg doctor .` verifies both halves are wired.
```

This detects which coding agents you have installed and writes **two** things
per agent: the MCP server config, and a short instruction block in the file that
agent actually reads — `CLAUDE.md` for Claude Code, `GEMINI.md` for Gemini,
`AGENTS.md` for Codex/Cursor/OpenCode, `.github/copilot-instructions.md` for
Copilot.

Both halves matter. An agent that has the tools registered but was never told
about them keeps grepping; the instruction block is what makes it reach for the
index instead. The block is fenced by `<!-- sg-block-version -->` markers, so
re-running `sg init` upgrades it in place and never touches your own notes, and
`sg uninstall` removes it cleanly.

**3. Restart your editor, and ask it something**

That's it. Your agent now has 15 new tools and will use the index instead of
grepping. You can check it works from the terminal first:

```bash
$ sg search "auth flow"
Authenticator.__init__ (method) — auth.py [score=0.266 sources=fts]
validate_token (function) — auth.py [score=0.216 sources=fts]
create_session (function) — auth.py [score=0.199 sources=fts]
handle_request (function) — api.py [score=0.032 sources=fts]
AdminAuthenticator.login (method) — admin.py [score=0.029 sources=fts]
```

> **Optional — better semantic search.** Out of the box symbolgraph uses
> full-text + graph search, which needs no model and scores `0.83` definition
> accuracy on the test fixture. If you have [Ollama](https://ollama.com)
> running, it's detected automatically and `sg embed` adds vector search,
> lifting that to `0.92`.

---

## Using it with your agent

After `sg init`, your agent has these tools. You never call them by hand — the
agent picks the right one — but it helps to know what it can do:

| Your agent can ask… | Tool |
|---|---|
| Where is this defined? | `definition` |
| What calls this? | `callers` |
| What does this call? | `callees` |
| What does this file import? | `imports` |
| Find code related to X | `search` |
| Give me context on X within N tokens | `context` |
| Is the index fresh? | `repository_status`, `index_repository` |
| Remember/recall decisions across a session | `session_*`, `record_decision`, `record_code_area` |

If your agent isn't picking them up: restart the editor, then run
`sg doctor .` (below).

<details>
<summary><b>Manual MCP setup</b> (if your agent wasn't auto-detected)</summary>

Add this to your agent's MCP config file:

```json
{
  "mcpServers": {
    "symbolgraph": { "command": "sg-mcp" }
  }
}
```

`sg init --agent all` writes this automatically for Claude Code, Cursor,
VS Code, OpenCode, Gemini, Copilot, Pi and Codex — each in that editor's own
shape (VS Code keys servers under `servers`, OpenCode wants an argv list).
</details>

---

## Everyday commands

These are the ones you'll actually use:

```bash
sg index .                       # build or update the index
sg search "how does login work"  # hybrid search
sg definition Authenticator      # where is it defined
sg callers login                 # who calls it
sg context "auth flow" --budget 800   # a token-budgeted context pack
sg status --oneline              # symbols 2026 chunks 2026 pending 0 gen 1
sg doctor .                      # check everything is healthy
sg watch .                       # keep the index fresh as you edit
```

`sg doctor` is the first thing to run when something seems off:

```bash
$ sg doctor .
✓ index present: .sg/index.sqlite
✓ lock free: free
✓ embedding queue: pending=0
✓ mcp registered: .mcp.json
✓ agent instructions: CLAUDE.md
✗ git hook: no hook — run `sg init`
✓ embedding backend: none - FTS+graph only (ok)
```

`mcp registered` and `agent instructions` are the two halves of `sg init`. If
your agent isn't calling the tools, check that second line first — a registered
server the agent was never told about produces zero tool calls, and that looks
exactly like the tools not working.

A `✗` is not necessarily a problem. Each line tells you the command that fixes
it. Running without Ollama is a supported setup, not a fault — search still
works, it just uses full-text plus the graph.

<details>
<summary><b>All 18 commands</b></summary>

| Command | What it does |
|---|---|
| `sg index <path> [--embed]` | Build/update the index; only changed files are re-parsed |
| `sg status [--oneline]` | Index generation and counts |
| `sg search <query> [--top-k N]` | Hybrid search across the index |
| `sg definition <name>` | Where a symbol is defined |
| `sg callers <name>` | Incoming `CALLS` edges |
| `sg callees <name>` | Outgoing `CALLS` edges |
| `sg imports <file>` | A file's imports, with resolved symbols |
| `sg context <query> [--budget N]` | Token-budgeted context pack |
| `sg eval [--embed]` | Run the fixed benchmark |
| `sg savings [--json]` | Token/$ figures from benchmark results |
| `sg watch <path>` | Reindex on file change (debounced) |
| `sg init [--agent auto\|all]` | Wire up MCP for detected agents + git hooks |
| `sg uninstall` | Remove MCP entries and hooks |
| `sg embed [--limit N]` | Drain the embedding queue |
| `sg doctor [--verbose]` | Health check |
| `sg dashboard [--port 8765]` | Local web UI on `127.0.0.1` |
| `sg sessions <start\|list\|recall\|…>` | Session memory |
| `sg eval-ab --manifest <file>` | Paired A/B task evaluation |

`sg --help` and `sg <command> --help` show full options. Override the database
location with `sg --db /tmp/x.sqlite <command>`.
</details>

---

## How it works

```text
Your repository
    │  respects .gitignore / .sgignore
    ▼
Scan + hash          only files whose hash changed go further
    │
    ▼
Parse once           tree-sitter builds one AST per file, reused by every pass
    │
    ▼
Extract symbols      every function/class/method gets a stable identity
    │                that survives edits and renames
    ▼
Resolve references   scope climbing, imports, re-exports, member paths
    │
    ▼
Build relationships  CALLS · EXTENDS · IMPLEMENTS · HAS_TYPE · RETURNS
    │
    ▼
Store                one SQLite file: symbols, edges, chunks, FTS5, vectors
    │
    ▼
Retrieve             exact + full-text + vector + graph expansion,
    │                fused by reciprocal rank, then reranked
    ▼
Answer               definitions and their relationships, inside a token budget
```

**Why "parse once" matters.** Each file is turned into a syntax tree a single
time and that tree is reused by every extraction pass. Re-parsing per feature is
the usual reason these tools get slow on big repos.

**Why identity matters.** Each symbol gets a `stable_key` derived from what it
*is*, not where it sits in the file. Add a line above a function and its
identity is unchanged — so a reindex can tell "this was edited" from "this is
new", and only touch what actually moved.

**What incremental actually does.** A Merkle hash over the tree finds the
changed subtree; unchanged files are never re-parsed. Measured on this
repository:

```bash
$ sg index .          # nothing edited since the last run
  parsed files:        0
  unchanged:           350
0.26 s

$ sg index .          # after editing one file
  parsed files:        1
  unchanged:           349
```

So a re-index costs a fraction of a second when nothing changed, and only the
files you actually touched get parsed again.

---

## What the benchmark says

Two separate things get measured, and they answer different questions.

### 1. Does it find the right code?

Measured on `tests/fixtures/evaluation_repo`, token budget 800. Reproduce with
`sg eval --embed`:

| Metric | Full-text + graph | With vectors |
|---|---|---|
| Definition accuracy | 0.83 | **0.92** |
| Mean recall@5 | 0.78 | **0.97** |
| MRR | 0.71 | **0.94** |
| Incremental reindex | `<50ms` | cache hit rate 1.0 |

The first column is what you get with no model installed at all.

### 2. Does it actually save tokens?

> ### 87% fewer context tokens
> Across **60 queries** on Django, Fiber and FastAPI — at **recall@10 0.95**,
> token budget 800. That is **382,064 → 48,925 tokens**, about **5,552 tokens
> saved per query**.

Reproduce it yourself with `sg savings` (the `(pooled)` row). The figure is
pooled over individual questions — every query's own baseline and context
summed, then one ratio — not an average of the three repos' percentages. At
sonnet input pricing that is about **$0.011 saved per query**.

**But one number hides the real finding** — savings depend almost entirely on
how big the file is.

A context pack has a fixed structure costing roughly 800 tokens. On a file
*smaller* than that, packing it costs more than just sending the file, and the
saving goes negative. Publishing one blended percentage would bury that, so the
results are segmented by file size:

| Baseline file size | Django | Fiber | FastAPI |
|---|---|---|---|
| **> 4k tokens** | **+93.9%** | **+90.4%** | **+94.4%** |
| 1k – 4k tokens | +65.8% | +53.3% | +60.3% |
| < 1k tokens | +11.3% | −21.1% | −293.3% |

Read that as: **on the large files where context actually hurts, symbolgraph
saves 90–94% of the tokens. On tiny files it costs you tokens** — so it is worth
using where files are big, which is exactly where agents struggle.

Whole-run aggregates, all of which cleared a recall gate declared *before* the
runs happened:

| Repo | Tokens/query | Saving | Recall@10 | p50 latency |
|---|---|---|---|---|
| Django | 8,909 → 811 | **90.9%** | 1.00 | 18.2ms |
| Fiber | 5,272 → 804 | **84.7%** | 0.95 | 9.3ms |
| FastAPI | 4,923 → 831 | **83.1%** | 0.90 | 3.8ms |

<details>
<summary><b>How these numbers are kept honest</b></summary>

- **Pre-registered.** The repos, their commit SHAs, the queries, the recall
  gate and the size buckets were all committed to git *before* the first run
  (`benchmarks/PREREGISTRATION.md`). The commit order is the proof — nothing
  was tuned after seeing a result.
- **The baseline is never the whole repo.** It is the content of the files a
  query was expected to need. Comparing a context pack against "read the entire
  repository" would manufacture a ~99% saving that means nothing;
  `evaluation/runner.py` hard-codes that number to `0.0` specifically to refuse
  it.
- **Savings are never quoted without recall.** A number that retrieves less is
  not a saving. Every row above passed a recall@10 gate of 0.90.
- **The aggregate is quoted, not the mean of ratios.** Averaging each query's
  own percentage lets a few tiny files swing the result wildly negative even
  when the set saves real tokens — on FastAPI the mean-of-ratios is `−1268%`
  for the same run whose true aggregate is `+83.1%`. The reported figure weights
  by actual token volume.
- **The losing rows are published.** The negative `<1k` numbers are printed
  above rather than dropped; they are what make the large-file rows believable.
- **What it measures, precisely.** This is the size of the retrieved context
  versus reading the files the query needed. It is *not* a measure of an
  agent's total cost to finish a task end to end — that would need paired
  agent runs, which are not done yet. Quote it as "fewer context tokens",
  not "87% cheaper agents".

Reproduce or verify:

```bash
sg savings                                              # read stored results
python benchmarks/run_external.py --recompute "benchmarks/results/*.json"
```

**Costs are a projection, not a measurement** — input tokens only, at
`sonnet $2.00/1M` as of `2026-06-24` (`retrieval/pricing.py`). Django at budget
800 works out to `$0.0162/query`. Always cite the model, price and date.
</details>

---

## Supported languages

**Full graph support** — symbols, typed relationships and imports:

| Language | Extensions |
|---|---|
| TypeScript / TSX | `.ts .tsx` |
| JavaScript / JSX | `.js .jsx` |
| Python | `.py` |
| Go | `.go` |
| Rust | `.rs` |
| Java | `.java` |
| C# | `.cs` |
| C / C++ | `.c .h .cpp .hpp .cc .hh .cxx .hxx` |

**Text-indexed** — searchable, but no syntax tree (26 more extensions):

`bash css dockerfile gql graphql hcl htm html json kt less md php proto rb scss
sh sql svelte swift tf toml vue xml yaml yml`

---

## Privacy and security

Your code stays on your machine. There is no account, no upload and no
telemetry. The only network call symbolgraph ever makes is to a local Ollama
instance, and only if you have one running.

Secrets are stripped **before** anything is indexed:

- **Files skipped entirely:** `.env*`, `credentials.json`, `secrets.yml`,
  `.pem` / `.key` / `.p12` / `.jks`.
- **Content redacted:** AWS keys, GitHub tokens, Slack tokens, Stripe keys,
  OpenAI / Anthropic / Google API keys, JWTs, private key blocks.
- **PII redacted:** emails, IPv4 addresses, phone numbers, SSNs, and card
  numbers that pass a Luhn check.
- **Obvious placeholders are left alone** — `your-api-key`, `test_key` and
  friends stay readable.

You can watch this work on symbolgraph itself: indexing this repository prints

```
Skipping .../indexing/secrets.py: contains secrets
```

because that file contains the example key patterns it matches on. It is
excluded from the index rather than redacted in place — files on disk are
never modified.

The dashboard binds to `127.0.0.1` only; exposing it needs both an explicit
flag and a token.

---

## Troubleshooting

**My agent isn't using it.**
Restart your editor after `sg init` — MCP servers are only picked up at startup.
Then `sg doctor .` to confirm the index and config exist.

**Search returns nothing.**
Check the index was actually built: `sg status --oneline`. If symbol counts are
`0`, run `sg index .` and watch for skipped files.

**Results feel stale.**
`sg index .` again, or `sg watch .` to keep it live. `sg init` also installs a
post-commit hook that refreshes it in the background.

**Semantic search isn't as good as advertised.**
The `0.92 / 0.97` figures need vectors. Install Ollama, then `sg embed`.
`sg doctor` reports which backend is active.

**Something is locked.**
`sg doctor` shows lock state. A stale lock lives at `.sg/.index.lock`.

**Start over.**
`rm -rf .sg/` removes the entire index. Your source is untouched — it is only
ever read.

---

## Project status

`0.1.0` is an early but genuinely working release: 677 tests passing at 80.68%
branch coverage, 11 language profiles, 15 MCP tools, and the benchmark numbers
above are reproducible from the commands given.

Known gaps, deliberately listed rather than hidden:

- Reranker weights are **tuned by grid search**, not learned from real agent
  runs. A real fit needs more (and larger) evaluation tasks than the current
  20 before it beats the tuned heuristic — an attempt on the current set
  regressed retrieval quality, so the tuned weights stand.
- `export *` wildcard re-exports resolve the file but not always the specific
  symbol.
- Vector search uses a flat index; no HNSW yet.

Issues and pull requests are the best place to see what's moving next.

---

## Understanding the internals

[`docs/`](docs/README.md) explains the whole system from first principles —
what each phase does, why it's built that way, what else we could have used,
and what each choice costs. Written to be read in order, with diagrams.

Start with [why a symbol graph, not chunks](docs/01-why-symbol-graph.md) — it's
the argument every other decision follows from.

---

## Contributing

Contributions are welcome — see `CONTRIBUTING.md`. The one rule that matters:

> If you can't point to the line and the test, it isn't done.

```bash
uv sync --all-extras
uv run pytest -q                          # 677 passed
uv run pytest --cov --cov-fail-under=80   # coverage gate
uv run ruff check .                       # lint
```

Security issues: please use GitHub's private vulnerability reporting rather than
a public issue. See `SECURITY.md`.

---

## License

MIT — see `LICENSE`.

<p align="center"><sub>If symbolgraph saves you tokens, a star helps. When citing a number, include the token budget and commit SHA.</sub></p>
