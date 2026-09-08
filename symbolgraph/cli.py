# pyright: reportImportCycles=false
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from embeddings.provider import EmbeddingProvider
from evaluation.runner import EvaluationReport, run_evaluation
from indexing.embedding_queue import queue_status, run_embedding_worker
from indexing.indexer import IndexRunReport, reindex_index
from models.entities.import_references import ImportReference
from models.entities.resolved_import_reference import ResolvedImportReference
from retrieval.context_builder import ContextPack
from retrieval.hybrid_retriever import HybridRetrieval
from retrieval.index_queries import (
    build_context_pack_from_index,
    build_hybrid_retriever,
)
from session_memory import SessionService
from storage.index_store import count_rows, current_generation

DEFAULT_DB_DIRNAME = ".sg"
DEFAULT_DB_FILENAME = "index.sqlite"


def _get_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("symbolgraph")
    except (ImportError, AttributeError, OSError, ValueError, KeyError):
        pass
    # fallback for source checkouts without installed metadata
    try:
        import tomllib  # Python 3.11+

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject.exists():
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
                return data.get("project", {}).get("version", "0.0.0+source")
    except (ImportError, OSError, KeyError, ValueError):
        pass
    return "0.0.0+source"


def _is_tty() -> bool:
    try:
        return sys.stderr.isatty()
    except (OSError, ValueError, AttributeError):
        return False


def _emit_progress(message: str) -> None:
    if _is_tty():
        print(message, file=sys.stderr)


def default_db_path(root: str) -> str:
    return str(Path(root) / DEFAULT_DB_DIRNAME / DEFAULT_DB_FILENAME)


def has_embeddings(db_path: str) -> bool:
    return count_rows(db_path)["embeddings"] > 0


# ---- commands (pure, testable independently of argument parsing) ----


def cmd_index(
    root: str,
    db_path: str,
    *,
    provider: EmbeddingProvider | None = None,
) -> IndexRunReport:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    if _is_tty():
        print(f"Indexing {root}...", file=sys.stderr)

    report = reindex_index(
        db_path, root, on_progress=_emit_progress if _is_tty() else None
    )

    if _is_tty():
        print(f"Parsed {report.parsed_files} files...", file=sys.stderr)

    if provider is not None:
        # drain embeddings in batches so large repos emit periodic progress
        # instead of appearing hung; suppress when stderr is not a TTY
        total = 0
        batch_size = 50
        while True:
            batch_report = run_embedding_worker(
                db_path,
                provider,
                limit=batch_size,
                on_progress=_emit_progress if _is_tty() else None,
            )
            if batch_report.claimed == 0:
                break
            total += batch_report.done
            if _is_tty():
                print(f"Embedded {total} chunks...", file=sys.stderr)
            if batch_report.claimed < batch_size:
                # check if any pending remain; if not, break, else continue
                try:
                    pending = queue_status(db_path)  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
                    runnable = pending.get("PENDING", 0) + max(
                        0, pending.get("FAILED", 0) - pending.get("exhausted", 0)
                    )
                    if runnable == 0:
                        break
                except Exception:
                    break

    return report


def cmd_status(db_path: str) -> dict[str, object]:
    from storage.index_store import count_rows

    counts = count_rows(db_path)

    return {
        "generation": current_generation(db_path),
        "documents": counts["documents"],  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
        "symbols": counts["symbols"],  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
        "chunks": counts["chunks"],  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
        "embeddings": counts["embeddings"],  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
        "embedding_jobs": queue_status(db_path),
    }


def cmd_search(
    db_path: str,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    top_k: int = 5,
) -> HybridRetrieval:
    retriever = build_hybrid_retriever(db_path, provider=provider)
    return retriever.retrieve(query, top_k=top_k)


def cmd_definition(db_path: str, name: str) -> HybridRetrieval:
    retriever = build_hybrid_retriever(db_path)
    return retriever.retrieve(f"where is {name} defined")


def cmd_callers(db_path: str, name: str) -> HybridRetrieval:
    retriever = build_hybrid_retriever(db_path)
    return retriever.retrieve(f"who calls {name}")


def cmd_callees(db_path: str, name: str) -> HybridRetrieval:
    retriever = build_hybrid_retriever(db_path)
    return retriever.retrieve(f"what does {name} call")


def cmd_imports(
    db_path: str,
    relative_path: str,
) -> list[tuple[ImportReference, ResolvedImportReference | None]]:
    from storage.index_store import load_imports_for_path

    return load_imports_for_path(db_path, relative_path)


def cmd_context(
    db_path: str,
    query: str,
    *,
    token_budget: int,
    provider: EmbeddingProvider | None = None,
    top_k: int = 5,
) -> ContextPack:
    return build_context_pack_from_index(
        db_path,
        query,
        token_budget=token_budget,
        provider=provider,
        top_k=top_k,
    )


def cmd_eval(
    *, provider: EmbeddingProvider | None = None, top_k: int = 5
) -> EvaluationReport:
    return run_evaluation(provider=provider, top_k=top_k)


def _benchmark_savings_rows(results_dir: str, model: str) -> list[dict[str, Any]]:
    """One row per (result file, budget, bucket) plus an overall row.

    Dollars are a projection from aggregate token means (input tokens only),
    never from the mean of per-query ratios. A bucket with no data reports
    null, not 0.0.
    """
    from retrieval.pricing import PRICE_DATE, dollars_saved, resolve_pricing

    price_in, _ = resolve_pricing(model)
    rows: list[dict[str, Any]] = []
    base = Path(results_dir)
    if not base.is_dir():
        return rows
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "total_questions" not in data:
            continue
        repo = data.get("repo", path.stem)
        blocks = dict(data.get("budgets", {}))
        blocks[str(data.get("token_budget", 800))] = data
        for budget_key, block in blocks.items():
            try:
                budget = int(budget_key)
            except (TypeError, ValueError):
                continue
            mean_baseline = block.get("mean_baseline_tokens", 0) or 0
            mean_context = block.get("mean_context_tokens", 0) or 0
            saved = mean_baseline - mean_context
            rows.append(
                {
                    "file": path.name,
                    "repo": repo,
                    "budget": budget,
                    "bucket": None,
                    "aggregate_pct": block.get("aggregate_savings_pct"),
                    "tokens_saved": saved,
                    "dollars_saved": dollars_saved(saved, model),
                    "model": model.lower(),
                    "price_in_per_1m": price_in,
                    "price_date": PRICE_DATE,
                    "recall_at_10": block.get("mean_recall_at_10"),
                }
            )
            for bucket_name, bucket in (block.get("buckets") or {}).items():
                if bucket.get("count", 0) == 0:
                    rows.append(
                        {
                            "file": path.name,
                            "repo": repo,
                            "budget": budget,
                            "bucket": bucket_name,
                            "aggregate_pct": None,
                            "tokens_saved": None,
                            "dollars_saved": None,
                            "model": model.lower(),
                            "price_in_per_1m": price_in,
                            "price_date": PRICE_DATE,
                            "recall_at_10": None,
                        }
                    )
                    continue
                b_saved = (bucket.get("mean_baseline_tokens", 0) or 0) - (
                    bucket.get("mean_context_tokens", 0) or 0
                )
                rows.append(
                    {
                        "file": path.name,
                        "repo": repo,
                        "budget": budget,
                        "bucket": bucket_name,
                        "aggregate_pct": bucket.get("aggregate_savings_pct"),
                        "tokens_saved": b_saved,
                        "dollars_saved": dollars_saved(b_saved, model),
                        "model": model.lower(),
                        "price_in_per_1m": price_in,
                        "price_date": PRICE_DATE,
                        "recall_at_10": bucket.get("mean_recall_at_10"),
                    }
                )
    rows.extend(_pooled_rows(base, model))
    return rows


# Repos that make up the pooled headline claim. The self-repo run is excluded
# on purpose: it is an 11-question sanity anchor on this codebase's own
# `retrieval/` package, not an independent repository, so folding it in would
# let us grade our own homework.
POOLED_REPOS = ("django", "fiber", "fastapi")


def _pooled_rows(base: Path, model: str) -> list[dict[str, Any]]:
    """One pooled row across POOLED_REPOS, at each repo's headline budget.

    Pooling is over individual questions — every query's own baseline and
    context tokens summed, then one ratio — not the mean of the three repos'
    percentages. Averaging percentages would weight a 20-question repo with
    small files the same as one with large files and quietly change the
    answer. Requires every pooled repo to be present, otherwise the number
    would silently describe a different set than it claims.
    """
    from retrieval.pricing import PRICE_DATE, dollars_saved, resolve_pricing

    price_in, _ = resolve_pricing(model)
    baseline = context = recall = 0.0
    questions = 0
    for name in POOLED_REPOS:
        path = base / f"{name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        for question in data.get("questions") or []:
            b = question.get("baseline_tokens")
            c = question.get("context_tokens")
            if b is None or c is None:
                continue
            baseline += b
            context += c
            recall += question.get("recall_at_10") or 0.0
            questions += 1
    if questions == 0 or baseline <= 0:
        return []

    saved = (baseline - context) / questions
    return [
        {
            "file": "(pooled)",
            "repo": "+".join(POOLED_REPOS),
            "budget": 800,
            "bucket": None,
            "aggregate_pct": 1.0 - context / baseline,
            "tokens_saved": saved,
            "dollars_saved": dollars_saved(saved, model),
            "model": model.lower(),
            "price_in_per_1m": price_in,
            "price_date": PRICE_DATE,
            "recall_at_10": recall / questions,
            "questions": questions,
        }
    ]


def cmd_savings(
    results_dir: str | None = None, *, model: str = "sonnet"
) -> list[dict[str, Any]]:
    """Summarize benchmark token/$ savings from tracked result files."""
    if results_dir is None:
        results_dir = str(Path(__file__).resolve().parent.parent / "benchmarks" / "results")
    return _benchmark_savings_rows(results_dir, model)


def cmd_watch(
    root: str,
    db_path: str,
    *,
    provider: EmbeddingProvider | None = None,
    debounce_seconds: float = 0.5,
    embed_limit: int | None = 200,
) -> None:
    from indexing.watcher import watch_repository

    watch_repository(
        root,
        db_path,
        provider=provider,
        debounce_seconds=debounce_seconds,
        embed_limit=embed_limit,
        on_report=lambda report: _print_index_report(report, db_path),
    )


def cmd_doctor(root: str = ".", verbose: bool = False) -> int:
    """One-command ops check: index, lock, git hook, embedding backend."""
    from pathlib import Path

    p = Path(root)
    db_path = default_db_path(root)
    checks: list[tuple[str, bool, str]] = []
    try:
        provider = _detect_provider()
    except Exception:
        provider = None
    # .sg exists
    has_db = Path(db_path).exists()
    checks.append(("index present", has_db, db_path if has_db else "no index — run `sg index .`"))
    # lock free
    try:
        from indexing.resource_governor import ProjectIndexLock

        lk = ProjectIndexLock(root)
        free = lk.acquire(blocking=False)
        if free:
            lk.release()
        checks.append(("lock free", free, "locked" if not free else "free"))
    except Exception as e:
        checks.append(("lock free", False, str(e)))
    # queue pending
    try:
        if has_db:
            qs = queue_status(db_path)  # type: ignore
            pending = qs.get("PENDING", 0) + qs.get("FAILED", 0) - qs.get("exhausted", 0)
            if provider is None:
                # Nothing can drain the queue without a backend, and running
                # without one is supported — don't report it as a fault.
                checks.append(("embedding queue", True, f"pending={pending} (no backend; not needed)"))
            else:
                checks.append(("embedding queue", pending == 0, f"pending={pending}"))
        else:
            checks.append(("embedding queue", True, "no db"))
    except Exception as e:
        checks.append(("embedding queue", False, str(e)))
    # MCP registration + agent instructions. A registered server the agent was
    # never told about produces zero tool calls, so both halves are checked.
    from symbolgraph.editors import EDITORS, SG_BLOCK_START

    wired = []
    for info in EDITORS.values():
        config = info.get("config_path")
        if not config:
            continue
        cfg_path = _resolve_editor_path(p, config)
        if not cfg_path.exists():
            continue
        try:
            if cfg_path.suffix == ".toml":
                from symbolgraph.editors import codex_section_name

                if f"[mcp_servers.{codex_section_name(p)}]" in cfg_path.read_text(errors="ignore"):
                    wired.append(config)
                continue
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if any(
                isinstance(data.get(k), dict) and "symbolgraph" in data[k]
                for k in ("mcpServers", "mcp", "servers")
            ):
                wired.append(config)
        except (json.JSONDecodeError, OSError):
            continue
    checks.append(
        (
            "mcp registered",
            bool(wired),
            ", ".join(wired) if wired else "no MCP config — run `sg init`",
        )
    )

    instructed = []
    for info in EDITORS.values():
        rel = info.get("instructions_path")
        if not rel:
            continue
        doc = p / rel
        if doc.exists() and SG_BLOCK_START in doc.read_text(errors="ignore"):
            instructed.append(rel)
    checks.append(
        (
            "agent instructions",
            bool(instructed),
            ", ".join(sorted(set(instructed)))
            if instructed
            else "no sg block — agent will keep grepping; run `sg init`",
        )
    )

    # git hook
    try:
        from indexing.git_hooks import _git_hooks_dir

        hd = _git_hooks_dir(p)
        hook = (hd / "hooks" / "post-commit") if hd else None
        has_hook = hook is not None and hook.exists() and "symbolgraph keep-fresh" in hook.read_text(errors="ignore")
        checks.append(("git hook", has_hook, str(hook) if has_hook else "no hook — run `sg init`"))
    except Exception:
        checks.append(("git hook", False, "unknown"))
    # embedding backend
    try:
        # No backend is a supported configuration (FTS + graph), not a failure:
        # failing it made `sg doctor` exit 1 on a perfectly healthy setup.
        checks.append(("embedding backend", True, provider.model_id if provider else "none - FTS+graph only (ok)"))
    except Exception as e:
        checks.append(("embedding backend", False, str(e)))

    ok = all(v for _, v, _ in checks)
    for name, passed, detail in checks:
        mark = "✓" if passed else "✗"
        print(f"{mark} {name}: {detail}")
        if verbose:
            pass
    return 0 if ok else 1


def _detect_provider(*, forced_model: str | None = None) -> EmbeddingProvider | None:
    """Auto-detect: Ollama if reachable, then local sentence-transformers if importable, else None.

    Respects SG_EMBED_BACKEND (ollama/local) and SG_OLLAMA_URL/MODEL env vars.
    Local import is lazy so core install never touches torch.
    """
    import os

    forced = os.environ.get("SG_EMBED_BACKEND", "").strip().lower()
    if forced and forced not in ("ollama", "local", "", "auto"):
        raise RuntimeError(
            f"Unknown embedding backend '{forced}'. Expected 'ollama' or 'local'. "
            "Unset SG_EMBED_BACKEND or set to 'ollama'/'local'."
        )

    # Ollama first (unless forced to local)
    if forced != "local":
        try:
            from embeddings.ollama_provider import (
                OllamaEmbeddingProvider,
                ollama_available,
            )

            # ollama_available uses env-var resolved URL internally
            if ollama_available():
                try:
                    return (
                        OllamaEmbeddingProvider(model_name=forced_model)
                        if forced_model
                        else OllamaEmbeddingProvider()
                    )
                except Exception:
                    # -- Ollama init may raise broad errors; fallback to local unless forced
                    if forced == "ollama":
                        raise
                    # fall through to local
            elif forced == "ollama":
                raise RuntimeError(
                    "Ollama backend forced via SG_EMBED_BACKEND=ollama but no Ollama server reachable at "
                    f"{os.environ.get('SG_OLLAMA_URL', os.environ.get('OLLAMA_HOST', 'http://localhost:11434'))}. "
                    "Start Ollama or unset SG_EMBED_BACKEND."
                )
        except ImportError:
            pass

    if forced != "ollama":
        try:
            import importlib.util

            if importlib.util.find_spec("sentence_transformers") is not None:
                from embeddings.local_provider import LocalEmbeddingProvider

                return (
                    LocalEmbeddingProvider(model_name=forced_model)
                    if forced_model
                    else LocalEmbeddingProvider()
                )
            elif forced == "local":
                raise RuntimeError(
                    "Local embeddings forced via SG_EMBED_BACKEND=local but 'sentence-transformers' not installed. "
                    "Install with: pip install symbolgraph[local]"
                )
        except RuntimeError:
            raise
        except (
            ImportError,
            OSError,
            ValueError,
        ) as e:  # -- provider detection fallback must not crash CLI
            if forced == "local":
                raise RuntimeError(
                    "Local embeddings require 'sentence-transformers'. "
                    "Install with: pip install symbolgraph[local]"
                ) from e
    return None


def resolve_provider(db_path: str, *, use_vector: bool) -> EmbeddingProvider | None:
    if not use_vector:
        return None
    # Default to no embeddings if none available — FTS+graph is out-of-box (0.83/0.78 fixture)
    # Caller decides legibility when vector was explicitly requested.
    try:
        return _detect_provider()
    except RuntimeError:
        raise
    except Exception:
        return None


def _require_provider_or_explain() -> EmbeddingProvider:
    provider = _detect_provider()
    if provider is None:
        raise RuntimeError(
            "No embedding backend available. Either:\n"
            "  1. Install local embeddings: pip install symbolgraph[local]\n"
            "  2. Start an Ollama server at http://localhost:11434 and pull nomic-embed-text (ollama pull nomic-embed-text)"
        )
    return provider


def _ensure_mcp_entry(
    path: Path, container_key: str | None, entry: dict[str, object] | None = None
) -> str:
    if container_key is None:
        return "written"
    if entry is None:
        entry = {"command": "sg-mcp"}
    if path.exists():
        # An unreadable or non-object config is the user's file, not ours to
        # replace: rewriting it would silently discard whatever they had.
        text = path.read_text(encoding="utf-8")

        try:
            data: dict[str, object] = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from error

        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")

        # check if already configured under any common container
        for key in (container_key, "mcpServers", "servers", "mcp"):
            container = data.get(key)
            if isinstance(container, dict) and "symbolgraph" in container:  # type: ignore[operator]
                return "already configured"
        container = data.get(container_key)  # type: ignore[assignment]
        if not isinstance(container, dict):
            data[container_key] = {}
            container = data[container_key]
        if "symbolgraph" in container:  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
            return "already configured"
        container["symbolgraph"] = entry  # type: ignore[index]  # pyright: ignore[reportIndexIssue,reportOperatorIssue]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return "written"
    else:
        data = {container_key: {"symbolgraph": entry}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return "written"


def _resolve_editor_path(root_path: Path, config: str) -> Path:
    """Editor config paths are repo-relative unless they start with ``~``."""
    if not config.startswith("~"):
        return root_path / config
    if config.startswith("~/.codex/"):
        from symbolgraph.editors import codex_config_path

        return codex_config_path()
    return Path(config).expanduser()


def _write_instruction_block(path: Path) -> str:
    """Add (or upgrade) the sg guidance block in an agent instruction file."""
    from symbolgraph.editors import atomic_write_text, ensure_block_content

    existing = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    new_content, already = ensure_block_content(existing)
    if already:
        return "already configured"
    atomic_write_text(path, new_content)
    return "written"


def _write_codex_entry(path: Path, root_path: Path) -> str:
    """Append this project's table to the global codex config, once."""
    from symbolgraph.editors import atomic_write_text, codex_section_name

    section = codex_section_name(root_path)
    header = f"[mcp_servers.{section}]"
    existing = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if header in existing:
        return "already configured"
    if existing and not existing.endswith("\n"):
        existing += "\n"
    atomic_write_text(path, f'{existing}\n{header}\ncommand = "sg-mcp"\n')
    return "written"


def cmd_init(root: str = ".", *, agents: list[str] | None = None) -> dict[str, str]:
    """Register the MCP server *and* tell each agent to use it.

    Writing the server config alone leaves the agent with tools it never calls;
    each editor also gets the guidance block in the instruction file it really
    reads (``CLAUDE.md`` for Claude Code, ``GEMINI.md`` for Gemini, and so on).
    """
    from symbolgraph.editors import EDITORS, detect_editors

    root_path = Path(root)
    if agents is None or agents == ["auto"]:
        agents = detect_editors(root_path)
        # Claude Code needs no marker file to be present, so always cover it.
        if "claude" not in agents:
            agents = ["claude"] + agents
    elif "all" in agents:
        agents = list(EDITORS.keys())

    results: dict[str, str] = {}
    # Several editors share an instruction file (AGENTS.md, copilot); write each
    # path once so a single run never reports "already configured" for a file it
    # just created.
    seen: set[Path] = set()

    def record(path: Path, status: str) -> None:
        if path in seen:
            return
        seen.add(path)
        results[str(path)] = status

    for agent in agents:
        info = EDITORS.get(agent)
        if not info:
            continue

        config = info.get("config_path")
        if config:
            path = _resolve_editor_path(root_path, config)
            if path not in seen:
                if path.suffix == ".toml":
                    record(path, _write_codex_entry(path, root_path))
                else:
                    record(path, _ensure_mcp_entry(path, info["container_key"], info.get("entry")))

        instructions = info.get("instructions_path")
        if instructions:
            path = _resolve_editor_path(root_path, instructions)
            if path not in seen:
                record(path, _write_instruction_block(path))

    # git hooks
    try:
        from indexing.git_hooks import install_hooks

        install_hooks(root_path)
    except Exception as error:
        results["git hooks"] = f"skipped ({error})"
    return results


def cmd_uninstall(root: str = ".") -> dict[str, str]:
    from indexing.git_hooks import uninstall_hooks
    from symbolgraph.editors import (
        EDITORS,
        atomic_write_text,
        codex_section_name,
        remove_block_content,
    )

    root_path = Path(root)
    removed: dict[str, str] = {}

    config_paths = {
        info["config_path"] for info in EDITORS.values() if info.get("config_path")
    }
    instruction_paths = {
        info["instructions_path"] for info in EDITORS.values() if info.get("instructions_path")
    }

    for name in sorted(config_paths):
        p = _resolve_editor_path(root_path, name)
        if not p.exists():
            continue
        if p.suffix == ".toml":
            # Drop only this project's table from the shared codex config.
            text = p.read_text(encoding="utf-8", errors="ignore")
            header = f"[mcp_servers.{codex_section_name(root_path)}]"
            if header not in text:
                continue
            kept: list[str] = []
            dropping = False
            for line in text.splitlines(keepends=True):
                stripped = line.strip()
                if stripped == header:
                    dropping = True
                    continue
                if dropping:
                    # A new table header ends our section; blank/key lines don't.
                    if stripped.startswith("[") and stripped.endswith("]"):
                        dropping = False
                    else:
                        continue
                kept.append(line)
            atomic_write_text(p, "".join(kept).strip("\n") + "\n")
            removed[str(p)] = "removed"
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        for k in ("mcpServers", "mcp", "servers"):
            container = data.get(k)
            if isinstance(container, dict) and "symbolgraph" in container:
                del container["symbolgraph"]
                changed = True
        if changed:
            p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            removed[str(p)] = "removed"

    for name in sorted(instruction_paths):
        p = _resolve_editor_path(root_path, name)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        new_text, stripped_block = remove_block_content(text)
        if not stripped_block:
            continue
        if new_text:
            atomic_write_text(p, new_text)
        else:
            # The file existed only to carry our block.
            p.unlink()
        removed[str(p)] = "removed"

    for h in uninstall_hooks(root_path):
        removed[h] = "removed"
    return removed


def cmd_embed(
    db_path: str,
    *,
    provider: EmbeddingProvider | None = None,
    limit: int | None = None,
):
    if provider is None:
        provider = _require_provider_or_explain()
    if limit is None:
        # when no explicit limit, drain in batches with periodic progress
        total = 0
        batch_size = 50
        last_report = None
        while True:
            batch_report = run_embedding_worker(
                db_path,
                provider,
                limit=batch_size,
                on_progress=_emit_progress if _is_tty() else None,
            )
            if batch_report.claimed == 0:
                if last_report is None:
                    return batch_report
                break
            last_report = batch_report
            total += batch_report.done
            if _is_tty():
                print(f"Embedded {total} chunks...", file=sys.stderr)
            # peek queue to decide continuation
            try:
                pending = queue_status(db_path)  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
                runnable = pending.get("PENDING", 0) + max(
                    0, pending.get("FAILED", 0) - pending.get("exhausted", 0)
                )
                if runnable == 0:
                    break
            except Exception:
                break
        return last_report if last_report is not None else batch_report
    return run_embedding_worker(
        db_path,
        provider,
        limit=limit,
        on_progress=_emit_progress if _is_tty() else None,
    )


def _background_lock_path(db_path: str) -> Path:
    return Path(db_path).parent / f".{Path(db_path).name}.embed.lock"


def _try_acquire_embed_lock(db_path: str, lease: int = 300) -> bool:
    lock = _background_lock_path(db_path)
    now = time.time()
    if lock.exists():
        try:
            mtime = lock.stat().st_mtime
            if now - mtime < lease:
                return False
            lock.unlink()
        except OSError:
            return False
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def _maybe_spawn_background_embed(db_path: str) -> None:
    # skip when queue empty (common after first run due to hash reuse)
    try:
        counts = queue_status(db_path)
        pending = counts.get("PENDING", 0)  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
        failed = counts.get("FAILED", 0)
        exhausted = counts.get("exhausted", 0)
        runnable = pending + max(0, failed - exhausted)
        if runnable == 0:
            return
    except Exception:
        return
    if not _try_acquire_embed_lock(db_path):
        return
    # Auto-detect backend for background drain; core install may have none
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "db_path=sys.argv[1]\n"
        "lock=Path(db_path).parent / f'.{Path(db_path).name}.embed.lock'\n"
        "try:\n"
        "    from symbolgraph.cli import _detect_provider\n"
        "    from indexing.embedding_queue import run_embedding_worker\n"
        "    provider=_detect_provider()\n"
        "    if provider is not None:\n"
        "        run_embedding_worker(db_path, provider)\n"
        "except Exception:\n"
        "    pass\n"
        "finally:\n"
        "    try:\n"
        "        lock.unlink()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, db_path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        try:
            _background_lock_path(db_path).unlink()
        except Exception:
            pass


# ---- output formatting ----


def _print_index_report(report: IndexRunReport, db_path: str) -> None:
    counts: dict[str, int] = {}
    for change in report.changes.values():
        counts[change.value] = counts.get(change.value, 0) + 1  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]

    print(f"Indexed into {db_path}")
    print(f"  parsed files:        {report.parsed_files}")
    print(f"  resolved references: {report.resolved_references}")
    for kind in ("new", "changed", "unchanged", "deleted"):
        if kind in counts:
            print(f"  {kind}: {counts[kind]}")  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]


def _print_status(
    status: dict[str, Any],
    *,
    oneline: bool = False,
) -> None:
    if oneline:
        jobs = cast(dict[str, int], status["embedding_jobs"])
        pending = jobs.get("PENDING", 0) + jobs.get("FAILED", 0) - jobs.get("exhausted", 0)
        pending = max(0, pending)
        print(
            f"symbols {status['symbols']} chunks {status['chunks']} pending {pending} gen {status['generation']}"
        )
        return
    print(f"generation: {status['generation']}")
    print(f"documents:  {status['documents']}")
    print(f"symbols:    {status['symbols']}")
    print(f"chunks:     {status['chunks']}")
    chunks = status["chunks"]  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
    embeddings = status["embeddings"]  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
    if chunks:
        pct = (int(embeddings) / int(chunks) * 100) if chunks else 0  # type: ignore[arg-type]  # pyright: ignore[reportOperatorIssue,reportArgumentType]
        if int(embeddings) < int(chunks):  # type: ignore[arg-type]  # pyright: ignore[reportOperatorIssue,reportArgumentType]
            print(
                f"embeddings: {embeddings}/{chunks} ({pct:.0f}%) — run `sg embed` to enable vector search"
            )
        else:
            print(f"embeddings: {embeddings}/{chunks} ({pct:.0f}%)")
    else:
        print(f"embeddings: {embeddings}")

    jobs = cast(dict[str, int], status["embedding_jobs"])
    if jobs:
        job_summary = ", ".join(f"{k}={v}" for k, v in sorted(jobs.items()))
        print(f"embedding queue: {job_summary}")
        pending = jobs.get("PENDING", 0) + jobs.get("FAILED", 0) - jobs.get("exhausted", 0)
        if pending > 0 and int(embeddings) < int(chunks):  # type: ignore[arg-type]  # pyright: ignore[reportOperatorIssue,reportArgumentType]
            print(
                f"vector search degraded: {pending} chunks pending embedding — run `sg embed`"
            )


def _print_embed_report(report) -> None:
    print(
        f"embeddings: claimed={report.claimed} done={report.done} reused={report.reused} stale={report.stale} failed={report.failed}"
    )


def _print_candidates(retrieval: HybridRetrieval) -> None:
    if not retrieval.candidates:
        print("No results.")
        return

    for candidate in retrieval.candidates:
        sources = ",".join(candidate.sources)
        print(
            f"{candidate.qualified_name or candidate.symbol_name} "
            f"({candidate.symbol_kind}) — {candidate.relative_path} "
            f"[score={candidate.score:.3f} sources={sources}]"
        )


def _print_imports(
    imports: list[tuple[ImportReference, ResolvedImportReference | None]],
) -> None:
    if not imports:
        print("No imports found.")
        return

    for import_reference, resolved in imports:
        target = "unresolved"

        if resolved is not None:
            target = resolved.target_document.relative_path
            if resolved.target_symbol is not None:
                target += f"::{resolved.target_symbol.name}"

        print(
            f"{import_reference.local_name} <- {import_reference.imported_name} "
            f'from "{import_reference.module_path}" -> {target}'
        )


def _print_context_pack(pack: ContextPack) -> None:
    print(f"context for: {pack.query}")
    print(f"tokens: {pack.total_tokens}/{pack.token_budget}")

    for label, entries in (
        ("primary", pack.primary_definitions),
        ("supporting", pack.supporting_definitions),
    ):
        for entry in entries:
            print(
                f"\n[{label}] {entry.qualified_name} ({entry.symbol_kind}) — {entry.location}"
            )
            if entry.source:
                print(entry.source)

    if pack.relationships:
        print("\nrelationships:")
        for relationship in pack.relationships:
            print(f"  {relationship}")


def _print_eval_report(report: EvaluationReport) -> None:
    print("Benchmark: fixed evaluation repo (tests/fixtures/evaluation_repo)\n")

    for question in report.questions:
        correct = (
            "-"
            if question.correct is None
            else ("PASS" if question.correct else "FAIL")
        )
        print(
            f"[{question.category:10}] {correct:4} "
            f"recall@k={question.recall_at_k:.2f} mrr={question.reciprocal_rank:.2f} "
            f"{question.text}"
        )

    print()
    print(f"definition accuracy:        {report.definition_accuracy:.2f}")
    print(f"relationship accuracy:      {report.relationship_accuracy:.2f}")
    print(f"import resolution accuracy: {report.import_resolution_accuracy:.2f}")
    print(f"mean recall@k:              {report.mean_recall_at_k:.2f}")
    print(f"mean reciprocal rank:       {report.mean_reciprocal_rank:.2f}")
    print(
        f"context tokens:             {report.context_tokens} (baseline {report.baseline_tokens})"
    )
    print(
        f"initial indexing:           {report.initial_indexing_seconds * 1000:.1f} ms"
    )
    print(
        f"incremental indexing:       {report.incremental_indexing_seconds * 1000:.1f} ms"
    )
    print(f"embedding cache hit rate:   {report.embedding_cache_hit_rate:.2f}")


# ---- argument parsing / dispatch ----


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symbolgraph",
        description="symbolgraph — local-first semantic index",
        epilog="examples:\n  sg init --agent all    # configure all editors\n  sg index .             # index repo\n  sg search \"login\"     # hybrid search\n  sg doctor .            # ops check\n  sg dashboard --no-browser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=_get_version())
    parser.add_argument("--db", help="override the index database path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index", help="build or update the semantic index"
    )
    index_parser.add_argument("path")
    index_parser.add_argument(
        "--embed", action="store_true", help="also run the embedding worker"
    )
    index_parser.add_argument(
        "--no-background",
        action="store_true",
        help="disable background embedding drain",
    )

    status_parser = subparsers.add_parser(
        "status", help="show index generation and counts"
    )
    status_parser.add_argument("path", nargs="?", default=".")
    status_parser.add_argument(
        "--oneline", action="store_true", help="one-line summary for shell prompts"
    )

    search_parser = subparsers.add_parser("search", help="hybrid search over the index")
    search_parser.add_argument("query")
    search_parser.add_argument("path", nargs="?", default=".")
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.add_argument("--no-vector", action="store_true")

    definition_parser = subparsers.add_parser(
        "definition", help="find where a symbol is defined"
    )
    definition_parser.add_argument("name")
    definition_parser.add_argument("path", nargs="?", default=".")

    callers_parser = subparsers.add_parser("callers", help="find callers of a symbol")
    callers_parser.add_argument("name")
    callers_parser.add_argument("path", nargs="?", default=".")

    callees_parser = subparsers.add_parser("callees", help="find callees of a symbol")
    callees_parser.add_argument("name")
    callees_parser.add_argument("path", nargs="?", default=".")

    imports_parser = subparsers.add_parser("imports", help="list a file's imports")
    imports_parser.add_argument("file")
    imports_parser.add_argument("path", nargs="?", default=".")

    context_parser = subparsers.add_parser(
        "context", help="build a token-budgeted context pack"
    )
    context_parser.add_argument("query")
    context_parser.add_argument("path", nargs="?", default=".")
    context_parser.add_argument("--budget", type=int, default=2000)
    context_parser.add_argument("--top-k", type=int, default=5)
    context_parser.add_argument("--no-vector", action="store_true")

    eval_parser = subparsers.add_parser(
        "eval", help="run the fixed benchmark and report retrieval/indexing metrics"
    )
    eval_parser.add_argument(
        "--embed", action="store_true", help="also exercise vector search"
    )
    eval_parser.add_argument("--top-k", type=int, default=5)

    watch_parser = subparsers.add_parser(
        "watch", help="keep the index fresh by watching for file changes"
    )
    watch_parser.add_argument("path")
    watch_parser.add_argument(
        "--no-embed",
        action="store_true",
        help="disable embedding worker after each reindex",
    )
    watch_parser.add_argument(
        "--debounce",
        type=float,
        default=0.5,
        help="seconds of edit quiet before reindexing (default 0.5)",
    )

    init_parser = subparsers.add_parser("init", help="configure MCP for this project")
    init_parser.add_argument("path", nargs="?", default=".")
    init_parser.add_argument("--agent", choices=["auto", "claude", "cursor", "vscode", "codex", "copilot", "pi", "opencode", "gemini", "all"], default="auto")
    init_parser.add_argument("--plugin", action="store_true", help="also generate plugin")
    uninstall_parser = subparsers.add_parser("uninstall", help="remove symbolgraph MCP configs and hooks")
    uninstall_parser.add_argument("path", nargs="?", default=".")

    embed_parser = subparsers.add_parser("embed", help="drain the embedding queue")
    embed_parser.add_argument("path", nargs="?", default=".")
    embed_parser.add_argument("--limit", type=int, default=None)

    doctor_parser = subparsers.add_parser("doctor", help="check index health, locks, hooks, and embedding backend")
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument("--verbose", action="store_true", help="show detailed checks")

    sessions = subparsers.add_parser(
        "sessions", help="manage local project session memory"
    )
    session_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    for name in ("start", "list"):
        p = session_sub.add_parser(name)
        p.add_argument("path", nargs="?", default=".")
    p = session_sub.add_parser("status")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("session_id", nargs="?")
    p = session_sub.add_parser("timeline")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("session_id", nargs="?")
    p.add_argument("--limit", type=int, default=50)
    p = session_sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--limit", type=int, default=10)
    p = session_sub.add_parser("export")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("session_id", nargs="?")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p = session_sub.add_parser("prune")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--days", type=int, required=True)

    dashboard = subparsers.add_parser(
        "dashboard", help="serve the local read-only dashboard"
    )
    dashboard.add_argument("path", nargs="?", default=".")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-browser", action="store_true")
    dashboard.add_argument(
        "--allow-remote", action="store_true", help="allow non-local binding (unsafe)"
    )

    savings_parser = subparsers.add_parser(
        "savings", help="summarize benchmark token/$ savings from tracked result files"
    )
    savings_parser.add_argument("--results-dir", default=None, help="benchmarks/results dir (default: checkout's)")
    savings_parser.add_argument("--model", default="sonnet", help="pricing model for $ projection (default: sonnet)")
    savings_parser.add_argument("--json", action="store_true", help="machine-readable output")

    ab = subparsers.add_parser("eval-ab", help="run the task-level symbolgraph A/B harness")
    ab.add_argument("--manifest", default="evaluation/tasks.json")
    ab.add_argument(
        "--condition", choices=("with_sg", "without_sg", "both"), default="both"
    )
    ab.add_argument("--output", default="results/")
    ab.add_argument("--dry-run", action="store_true")
    ab.add_argument("--agent-command")
    ab.add_argument(
        "--pilot",
        action="store_true",
        help="run exactly one Python and one JavaScript task",
    )
    ab.add_argument(
        "--preflight",
        action="store_true",
        help="validate paired symbolgraph provisioning without launching an agent",
    )
    ab.add_argument(
        "--timeout",
        type=int,
        help="per-run timeout in seconds, overriding each task's manifest value",
    )

    return parser


def _require_dir(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"path '{path}' does not exist")
    if not p.is_dir():
        raise NotADirectoryError(f"path '{path}' is not a directory")


def main(argv: list[str] | None = None) -> int:  # pyright: ignore[reportGeneralTypeIssues]
    args = build_parser().parse_args(argv)

    try:
        # validate target paths before dispatch
        if (
            args.command in ("index", "watch")
            or args.command not in ("eval",)
            and hasattr(args, "path")
        ):
            _require_dir(args.path)

        if args.command == "eval-ab":
            from evaluation.ab_runner import main as ab_main

            return ab_main(
                [
                    "--manifest",
                    args.manifest,
                    "--condition",
                    args.condition,
                    "--output",
                    args.output,
                ]
                + (["--dry-run"] if args.dry_run else [])
                + (["--pilot"] if args.pilot else [])
                + (["--preflight"] if args.preflight else [])
                + (
                    ["--agent-command", args.agent_command]
                    if args.agent_command
                    else []
                )
                + (["--timeout", str(args.timeout)] if args.timeout else [])
            )

        if args.command == "doctor":
            return cmd_doctor(args.path, verbose=args.verbose)

        if args.command == "dashboard":
            if (
                args.host not in ("127.0.0.1", "localhost", "::1")
                and not args.allow_remote
            ):
                print(
                    "Refusing remote dashboard binding without --allow-remote",
                    file=sys.stderr,
                )
                return 1
            import webbrowser

            from symbolgraph.dashboard.server import create_server

            server = create_server(args.path, args.host, args.port)
            print(
                f"symbolgraph dashboard: http://{args.host}:{args.port}/ (local read-only; do not expose publicly)"
            )
            if not args.no_browser:
                webbrowser.open(f"http://{args.host}:{args.port}/")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0

        if args.command == "sessions":
            service = SessionService(args.path)
            command = args.sessions_command
            if command == "start":
                result = service.start()
            elif command == "list":
                result = service.list()
            elif command == "status":
                result = service.status(args.session_id)
            elif command == "timeline":
                result = service.timeline(args.session_id, args.limit)
            elif command == "recall":
                result = service.recall(args.query, args.limit)
            elif command == "export":
                output = service.export(args.session_id, args.format)
                print(output)
                return 0
            else:
                result = service.prune(args.days)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if args.command == "index":
            db_path = args.db or default_db_path(args.path)
            provider = None

            if args.embed:
                provider = _require_provider_or_explain()

            report = cmd_index(args.path, db_path, provider=provider)
            _print_index_report(report, db_path)
            if not args.no_background and not args.embed:
                _maybe_spawn_background_embed(db_path)
            return 0

        if args.command == "eval":
            provider = None

            if args.embed:
                provider = _require_provider_or_explain()

            _print_eval_report(cmd_eval(provider=provider, top_k=args.top_k))
            return 0

        if args.command == "init":
            try:
                results = cmd_init(args.path, agents=[args.agent])
            except (ValueError, OSError) as error:
                print(f"Could not configure MCP: {error}", file=sys.stderr)
                return 1

            for file_path, status in results.items():
                if status == "already configured":
                    print(f"already configured: {file_path}")
                elif status.startswith("skipped"):
                    print(f"{status}: {file_path}")
                else:
                    print(f"Wrote {file_path}")
            print()
            print("Next: run `sg index .`, then restart your editor so it picks")
            print("up the MCP server. `sg doctor .` verifies both halves are wired.")
            return 0

        if args.command == "uninstall":
            results = cmd_uninstall(args.path)
            for p, s in results.items():
                print(f"{s}: {p}")
            return 0

        if args.command == "savings":
            from retrieval.pricing import PRICE_DATE

            rows = cmd_savings(args.results_dir, model=args.model)
            if not rows:
                print("No benchmark result files found. Run benchmarks/run_external.py first.")
                return 1
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            print(f"Dollars are a projection (input tokens only, {args.model} pricing as of {PRICE_DATE}).")
            current: tuple = ("", 0)
            for r in rows:
                key = (r["file"], r["budget"])
                if key != current:
                    current = key
                    print(f"\n{r['file']} budget {r['budget']}:")
                if r["bucket"] is None:
                    pct = r["aggregate_pct"]
                    pct_s = f"{pct * 100:.1f}%" if pct is not None else "n/a"
                    rec = r["recall_at_10"]
                    rec_s = f"{rec:.2f}" if rec is not None else "n/a"
                    print(
                        f"  overall: {pct_s} aggregate "
                        f"({r['tokens_saved']:.0f} tokens/query, "
                        f"${r['dollars_saved']:.4f}/query @ R@10 {rec_s})"
                    )
                elif r["aggregate_pct"] is None:
                    print(f"  bucket {r['bucket']}: no data")
                else:
                    print(
                        f"  bucket {r['bucket']}: {r['aggregate_pct'] * 100:.1f}% "
                        f"({r['tokens_saved']:.0f} tokens/query, "
                        f"${r['dollars_saved']:.4f}/query @ R@10 {r['recall_at_10']:.2f})"
                    )
            return 0

        db_path = args.db or default_db_path(args.path)

        if args.command == "watch":
            if args.no_embed:
                provider = None
            else:
                provider = _detect_provider()

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            print(f"Watching {args.path} (index: {db_path})")
            cmd_watch(
                args.path,
                db_path,
                provider=provider,
                debounce_seconds=args.debounce,
                embed_limit=200,
            )
            return 0

        if not Path(db_path).exists():
            print(f"No index found at {db_path}. Run `sg index {args.path}` first.")
            return 1

        if args.command == "embed":
            # cmd_embed will auto-detect; if no backend, it raises legible error
            report = cmd_embed(db_path, limit=args.limit)
            _print_embed_report(report)
        elif args.command == "status":
            _print_status(cmd_status(db_path), oneline=getattr(args, "oneline", False))
        elif args.command == "search":
            provider = resolve_provider(db_path, use_vector=not args.no_vector)
            if provider is None and not args.no_vector:
                # Legible failure when no backend at all
                try:
                    backend = _detect_provider()
                    if backend is None:
                        print(
                            "Vector search disabled: no embedding backend available. "
                            "Install with 'pip install symbolgraph[local]' "
                            "or start Ollama at http://localhost:11434 (ollama pull nomic-embed-text).",
                            file=sys.stderr,
                        )
                    else:
                        qs = queue_status(db_path)
                        pending = (
                            qs.get("PENDING", 0)
                            + qs.get("FAILED", 0)
                            - qs.get("exhausted", 0)
                        )  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
                        if pending > 0:
                            print(
                                f"vector search inactive: {pending} chunks pending embedding — run `sg embed`"
                            )
                except Exception:
                    pass
            _print_candidates(
                cmd_search(db_path, args.query, provider=provider, top_k=args.top_k)
            )
        elif args.command == "definition":
            _print_candidates(cmd_definition(db_path, args.name))
        elif args.command == "callers":
            _print_candidates(cmd_callers(db_path, args.name))
        elif args.command == "callees":
            _print_candidates(cmd_callees(db_path, args.name))
        elif args.command == "imports":
            _print_imports(cmd_imports(db_path, args.file))
        elif args.command == "context":
            provider = resolve_provider(db_path, use_vector=not args.no_vector)
            if provider is None and not args.no_vector:
                try:
                    backend = _detect_provider()
                    if backend is None:
                        print(
                            "Vector search disabled: no embedding backend available. "
                            "Install with 'pip install symbolgraph[local]' "
                            "or start Ollama at http://localhost:11434 (ollama pull nomic-embed-text).",
                            file=sys.stderr,
                        )
                    else:
                        qs = queue_status(db_path)
                        pending = (
                            qs.get("PENDING", 0)
                            + qs.get("FAILED", 0)
                            - qs.get("exhausted", 0)
                        )  # pyright: ignore[reportAttributeAccessIssue,reportOperatorIssue,reportIndexIssue]
                        if pending > 0:
                            print(
                                f"vector search inactive: {pending} chunks pending embedding — run `sg embed`"
                            )
                except Exception:
                    pass
            _print_context_pack(
                cmd_context(
                    db_path,
                    args.query,
                    token_budget=args.budget,
                    provider=provider,
                    top_k=args.top_k,
                )
            )

        return 0
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except NotADirectoryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        db = locals().get("db_path", "index")
        print(
            f"Database error at {db}: {exc} — try removing the database and re-running `sg index`.",
            file=sys.stderr,
        )
        return 1
    except ImportError as exc:
        print(f"Embeddings unavailable: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        if getattr(args, "embed", False) or args.command in ("embed", "eval"):
            print(f"Embeddings unavailable: {exc}", file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        if getattr(args, "embed", False) or args.command in ("embed", "eval"):
            print(f"Embeddings unavailable: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
