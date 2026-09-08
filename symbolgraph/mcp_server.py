from pathlib import Path
from time import perf_counter
from typing import Any

from mcp.server.mcpserver import MCPServer

from session_memory import SessionService
from symbolgraph.cli import (
    cmd_callees,
    cmd_callers,
    cmd_context,
    cmd_definition,
    cmd_imports,
    cmd_index,
    cmd_search,
    cmd_status,
    default_db_path,
    has_embeddings,
    resolve_provider,
)

_mcp_provider = None

# Resource governor — idle tracking for long-lived MCP server (P4-1-1)
try:
    from indexing.resource_governor import IdleTracker

    _idle_tracker = IdleTracker(timeout_seconds=1800)
except Exception:
    _idle_tracker = None  # type: ignore[assignment]


def _touch_idle() -> None:
    if _idle_tracker is not None:
        try:
            _idle_tracker.touch()
        except Exception:
            pass


def _get_mcp_provider():
    global _mcp_provider
    if _mcp_provider is None:
        try:
            # Auto-detect: Ollama if reachable, then local
            from symbolgraph.cli import _detect_provider

            _mcp_provider = _detect_provider()
        except Exception:
            return None
    return _mcp_provider


def set_mcp_provider(provider) -> None:
    """Inject or clear the memoized MCP embedding provider (for tests)."""
    global _mcp_provider
    _mcp_provider = provider


mcp = MCPServer(
    name="symbolgraph",
    instructions=(
        "symbolgraph is a local index of this repository's symbols and the "
        "relationships between them (TypeScript, JavaScript, TSX, JSX, Python, "
        "Go).\n\n"
        "Use these tools INSTEAD of grep/glob/file-reads when locating code:\n"
        "  definition(name)  - where a symbol is defined\n"
        "  callers(name)     - what calls it\n"
        "  callees(name)     - what it calls\n"
        "  imports(file)     - a file's imports and where they resolve\n"
        "  search(query)     - open-ended 'where does X happen?'\n"
        "  context(query)    - the same, returning code within a token budget\n\n"
        "Call index_repository(path) once per repository before the others; it "
        "is incremental and cheap to re-run after edits. Fall back to reading "
        "files only when a tool returns no results."
    ),
)


def _not_indexed(path: str, db_path: str) -> dict[str, Any]:
    return {
        "error": f'No index found at {db_path}. Call index_repository(path="{path}") first.'
    }


def _candidate_dict(candidate: Any) -> dict[str, Any]:
    return {
        "symbol_name": candidate.symbol_name,
        "qualified_name": candidate.qualified_name,
        "kind": candidate.symbol_kind,
        "relative_path": candidate.relative_path,
        "score": candidate.score,
        "sources": list(candidate.sources),
    }


def _import_dict(pair: Any) -> dict[str, Any]:
    import_reference, resolved = pair
    entry: dict[str, Any] = {
        "module_path": import_reference.module_path,
        "imported_name": import_reference.imported_name,
        "local_name": import_reference.local_name,
        "resolved": resolved is not None,
    }

    if resolved is not None:
        entry["target_file"] = resolved.target_document.relative_path
        entry["target_symbol"] = (
            resolved.target_symbol.name if resolved.target_symbol is not None else None
        )

    return entry


def _context_entry_dict(entry: Any) -> dict[str, Any]:
    return {
        "qualified_name": entry.qualified_name,
        "kind": entry.symbol_kind,
        "relative_path": entry.relative_path,
        "location": entry.location,
        "role": entry.role,
        "source": entry.source,
    }


def is_idle() -> bool:
    if _idle_tracker is None:
        return False
    try:
        return _idle_tracker.is_idle()
    except Exception:
        return False


@mcp.tool()
async def index_repository(path: str, embed: bool = False) -> dict[str, Any]:
    """Build or update the semantic index for a repository at `path`.

    Must be called (once) before any other tool is used against that path.
    Cheap and safe to call again after edits - only changed files are
    reprocessed. When called from a long-lived MCP server it also drains a
    small batch of embedding jobs lazily so vector search becomes available
    without blocking the index call.
    """
    db_path = default_db_path(path)
    provider = None

    if embed:
        from embeddings.local_provider import LocalEmbeddingProvider

        provider = LocalEmbeddingProvider()

    _touch_idle()
    report = cmd_index(path, db_path, provider=provider)

    # Long-lived server can absorb the 14s model load; drain a bounded batch
    # lazily without blocking import. Limit keeps a single call from stalling.
    # Backoff under memory pressure (P4-1-1)
    try:
        lazy_provider = provider if provider is not None else _get_mcp_provider()
        if lazy_provider is not None:
            from indexing.embedding_queue import run_embedding_worker
            from indexing.resource_governor import is_memory_pressured

            batch = 100 if is_memory_pressured() else 200
            run_embedding_worker(db_path, lazy_provider, limit=batch)
    except Exception:
        pass

    return {
        "db_path": db_path,
        "parsed_files": report.parsed_files,
        "resolved_references": report.resolved_references,
        "changes": {p: c.value for p, c in report.changes.items()},
    }


@mcp.tool()
async def repository_status(path: str = ".") -> dict[str, Any]:
    """Report index generation and document/symbol/chunk/embedding counts for `path`."""
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return {"indexed": False}

    return {"indexed": True, **cmd_status(db_path)}


@mcp.tool()
async def definition(name: str, path: str = ".") -> dict[str, Any]:
    """Find the exact definition site(s) of a symbol by name."""
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    retrieval = cmd_definition(db_path, name)
    return {"results": [_candidate_dict(c) for c in retrieval.candidates]}


@mcp.tool()
async def callers(name: str, path: str = ".") -> dict[str, Any]:
    """Find every symbol that calls `name` (1-hop incoming graph neighborhood)."""
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    retrieval = cmd_callers(db_path, name)
    return {"results": [_candidate_dict(c) for c in retrieval.candidates]}


@mcp.tool()
async def callees(name: str, path: str = ".") -> dict[str, Any]:
    """Find every symbol that `name` calls (1-hop outgoing graph neighborhood)."""
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    retrieval = cmd_callees(db_path, name)
    return {"results": [_candidate_dict(c) for c in retrieval.candidates]}


@mcp.tool()
async def search(query: str, path: str = ".", top_k: int = 5) -> dict[str, Any]:
    """Hybrid search: lexical (FTS) + exact-symbol + graph expansion + reranking;
    vector similarity is used when embeddings are available (run `sg embed`
    to generate them). Use for open-ended questions; use definition/callers/callees
    for exact lookups instead.
    """
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    # Prefer injected/test provider to avoid constructing real model in tests
    if _mcp_provider is not None and has_embeddings(db_path):
        provider = _mcp_provider
    else:
        provider = resolve_provider(db_path, use_vector=True)
    started = perf_counter()
    retrieval = cmd_search(db_path, query, provider=provider, top_k=top_k)
    # pending count for self-diagnosis
    try:
        from indexing.embedding_queue import queue_status

        qs = queue_status(db_path)
        pending = qs.get("PENDING", 0) + qs.get("FAILED", 0) - qs.get("exhausted", 0)
    except Exception:
        pending = 0

    result: dict[str, Any] = {
        "results": [_candidate_dict(c) for c in retrieval.candidates],
        "vector_search_used": provider is not None,
        "pending_embeddings": pending,
    }
    if result["results"]:
        try:
            SessionService(path).retrieval(query, [f"{x['relative_path']}:{x['qualified_name']}" for x in result["results"]], 0, 0, (perf_counter()-started)*1000)
        except Exception:
            pass
    return result


@mcp.tool()
async def imports(file: str, path: str = ".") -> dict[str, Any]:
    """List a file's own import statements and where each one resolves to."""
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    return {"imports": [_import_dict(pair) for pair in cmd_imports(db_path, file)]}


@mcp.tool()
async def context(query: str, path: str = ".", token_budget: int = 2000, top_k: int = 5) -> dict[str, Any]:
    """Build a token-budgeted context pack: primary/supporting definitions
    with source excerpts and relationships. Uses vector search when embeddings
    are available (run `sg embed` otherwise).
    """
    _touch_idle()
    db_path = default_db_path(path)

    if not Path(db_path).exists():
        return _not_indexed(path, db_path)

    if _mcp_provider is not None and has_embeddings(db_path):
        provider = _mcp_provider
    else:
        provider = resolve_provider(db_path, use_vector=True)
    started = perf_counter()
    pack = cmd_context(db_path, query, token_budget=token_budget, provider=provider, top_k=top_k)
    try:
        from indexing.embedding_queue import queue_status

        qs = queue_status(db_path)
        pending = qs.get("PENDING", 0) + qs.get("FAILED", 0) - qs.get("exhausted", 0)
    except Exception:
        pending = 0

    result: dict[str, Any] = {
        "query": pack.query,
        "token_budget": pack.token_budget,
        "total_tokens": pack.total_tokens,
        "baseline_tokens": getattr(pack, "baseline_tokens", 0),
        "primary_definitions": [_context_entry_dict(e) for e in pack.primary_definitions],
        "supporting_definitions": [_context_entry_dict(e) for e in pack.supporting_definitions],
        "relationships": list(pack.relationships),
        "file_paths": list(pack.file_paths),
        "vector_search_used": provider is not None,
        "pending_embeddings": pending,
    }
    selected = result["primary_definitions"] + result["supporting_definitions"]
    if selected:
        try:
            baseline = int(getattr(pack, "baseline_tokens", 0) or 0)
            SessionService(path).retrieval(query, [f"{x['relative_path']}:{x['qualified_name']}" for x in selected], pack.total_tokens, baseline, (perf_counter()-started)*1000)
        except Exception:
            pass
    return result


def _session_error(error: Exception) -> dict[str, Any]:
    return {"error": str(error)}


@mcp.tool()
async def session_start(path: str = ".") -> dict[str, Any]:
    _touch_idle()
    return {"session": SessionService(path).start()}


@mcp.tool()
async def session_end(path: str, session_id: str) -> dict[str, Any]:
    _touch_idle()
    session = SessionService(path).end(session_id)
    return {"session": session} if session else _session_error(ValueError("session not found for project"))


@mcp.tool()
async def session_status(path: str, session_id: str | None = None) -> dict[str, Any]:
    _touch_idle()
    return {"session": SessionService(path).status(session_id)}


@mcp.tool()
async def session_recall(path: str, query: str, limit: int = 10) -> dict[str, Any]:
    _touch_idle()
    return {"results": SessionService(path).recall(query, limit)}


@mcp.tool()
async def session_timeline(path: str, session_id: str, limit: int = 50) -> dict[str, Any]:
    _touch_idle()
    try: return {"session_id": session_id, "events": SessionService(path).timeline(session_id, limit)}
    except ValueError as error: return _session_error(error)


@mcp.tool()
async def record_decision(path: str, decision: str, reason: str = "", session_id: str | None = None) -> dict[str, Any]:
    _touch_idle()
    try: return {"decision": SessionService(path).decision(decision, reason, session_id)}
    except ValueError as error: return _session_error(error)


@mcp.tool()
async def record_code_area(path: str, file_path: str, description: str = "", session_id: str | None = None) -> dict[str, Any]:
    _touch_idle()
    try: return {"code_area": SessionService(path).code_area(file_path, description, session_id)}
    except ValueError as error: return _session_error(error)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
