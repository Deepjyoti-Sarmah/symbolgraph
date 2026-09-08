<!-- sg-block-version: 2 -->
## symbolgraph — query the code index before reading files

This repository is indexed by symbolgraph, and the `symbolgraph` MCP server is
configured for it. Its tools answer code-structure questions directly, from a
local index, without opening files.

**Before using grep/glob/file-reads to locate code, call the matching tool:**

| Question | Tool |
|---|---|
| Where is `X` defined? | `definition(name="X")` |
| What calls `X`? | `callers(name="X")` |
| What does `X` call? | `callees(name="X")` |
| What does this file import, and where does it resolve? | `imports(file="...")` |
| Open-ended ("where does auth happen?") | `search(query="...")` |
| Same, but return the code within a token budget | `context(query="...", token_budget=2000)` |

Rules:

- Call `index_repository(path=".")` once per session before the other tools.
  It is incremental — re-running it after edits is cheap.
- Only fall back to grep or reading whole files when a tool returns no results.
- Never read a whole file just to answer "where is this defined" or "what calls
  this" — that is exactly what `definition`/`callers` are for.

Terminal equivalents, if you are running shell commands rather than tools:
`sg index .`, `sg search "..."`, `sg context "..."`, `sg definition X`,
`sg callers X`, `sg callees X`.
<!-- end sg-block -->
