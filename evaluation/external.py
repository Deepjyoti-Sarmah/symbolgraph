"""External benchmark harness — file-level ground truth from a query JSON file.

Ground truth is expected_files (paths relative to source_dir), not symbol
names.  Candidates are chunks; several can share a file, so dedupe to
file paths and retrieve wider (top_k=30) to yield min(10, distinct files
available) distinct files.
"""
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from embeddings.provider import EmbeddingProvider
from evaluation.metrics import mean, recall_at_k, reciprocal_rank, token_reduction
from indexing.embedding_queue import run_embedding_worker
from indexing.indexer import reindex_index
from retrieval.context_builder import build_context_pack, estimate_tokens
from retrieval.index_queries import build_hybrid_retriever
from storage.index_store import load_index


@dataclass(frozen=True, slots=True)
class ExternalQuestion:
    query: str
    expected_files: frozenset[str]
    category: str = ""


@dataclass(slots=True)
class ExternalQuestionResult:
    query: str
    expected_files: frozenset[str]
    category: str
    ranked_files: list[str]  # deduped distinct files in rank order
    precision_at_10: float
    recall_at_10: float
    reciprocal_rank: float
    latency_seconds: float
    precision_ceiling_at_10: float = 0.0
    precision_at_10_normalized: float = 0.0
    precision_over_returned: float = 0.0
    # honest token-savings: baseline is ground-truth files, actual is context pack
    baseline_tokens: int = 0
    context_tokens: int = 0
    savings_pct: float = 0.0
    # The files that actually survived the token budget into the pack, and
    # the fraction of expected_files among them. recall_at_10 scores the
    # ranked candidate list, which is computed before the budget is applied
    # and so cannot fall when the budget is cut; this scores the artifact the
    # saving is claimed on. See ExternalReport.mean_pack_recall.
    pack_files: tuple[str, ...] = ()
    pack_recall: float = 0.0


@dataclass(slots=True)
class ExternalReport:
    repo: str
    source_dir: str
    commit: str | None
    questions: list[ExternalQuestionResult] = field(default_factory=list)
    mean_precision_at_10: float = 0.0
    mean_recall_at_10: float = 0.0
    mean_reciprocal_rank: float = 0.0
    mean_precision_ceiling_at_10: float = 0.0
    mean_precision_at_10_normalized: float = 0.0
    mean_precision_over_returned: float = 0.0
    p50_latency_seconds: float = 0.0
    p95_latency_seconds: float = 0.0
    index_seconds: float = 0.0
    total_questions: int = 0
    # honest token-savings aggregates (paired with recall)
    mean_baseline_tokens: float = 0.0
    mean_context_tokens: float = 0.0
    # Mean of each query's own savings_pct. This is a mean of ratios with
    # very different denominators (a 40-token file and a 3,800-token file
    # both count once), so a handful of small files where the fixed
    # context-pack overhead exceeds the file's own size can swing this
    # deeply negative even when the set saves tokens in aggregate. Useful
    # for "what does a typical single query look like", not for a
    # headline number.
    mean_savings_pct: float = 0.0
    # 1 - mean_context_tokens / mean_baseline_tokens — equivalent to
    # summing baseline and context tokens across every query and taking
    # one ratio, so each query is weighted by its actual token volume
    # instead of counted once regardless of size. This is the number a
    # savings claim should cite.
    aggregate_savings_pct: float = 0.0
    # Mean fraction of a query's expected_files present in the context pack
    # after the budget is applied. mean_recall_at_10 and this metric measure
    # two different objects: the first scores `ranked_files` (pre-budget), the
    # second scores `pack.file_paths` (post-budget). Only the second can
    # respond to token_budget, so only the second can gate a savings claim --
    # a saving produced by truncation shows up here as a recall drop and
    # nowhere else.
    mean_pack_recall: float = 0.0


def load_external_questions(path: str | Path) -> list[ExternalQuestion]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions: list[ExternalQuestion] = []
    for entry in data:
        query = entry.get("query", "")
        expected = entry.get("expected_files", [])
        category = entry.get("category", "")
        questions.append(
            ExternalQuestion(
                query=query,
                expected_files=frozenset(expected),
                category=category,
            )
        )
    return questions


def _dedupe_files(candidates, limit: int = 10) -> list[str]:
    """Deduplicate chunk candidates to distinct file paths in rank order."""
    seen: set[str] = set()
    files: list[str] = []
    for c in candidates:
        fp = c.relative_path
        if fp not in seen:
            seen.add(fp)
            files.append(fp)
            if len(files) >= limit:
                break
    return files


def _precision_at_k(expected: frozenset[str], ranked: list[str], k: int = 10) -> float:
    if not expected or k == 0:
        return 0.0
    top = ranked[:k]
    hits = sum(1 for f in top if f in expected)
    return hits / k


def _precision_ceiling_at_k(expected: frozenset[str], k: int = 10) -> float:
    if k == 0:
        return 0.0
    return min(len(expected), k) / k


def _precision_over_returned(expected: frozenset[str], ranked: list[str]) -> float:
    if not ranked:
        return 0.0
    hits = sum(1 for f in ranked if f in expected)
    return hits / len(ranked)


def run_external_evaluation(
    repo_dir: str | Path,
    questions: list[ExternalQuestion],
    *,
    provider: EmbeddingProvider | None = None,
    top_k: int = 30,
    file_k: int = 10,
    db_path: str | Path | None = None,
    token_budget: int = 800,
) -> ExternalReport:
    """Run file-level evaluation over a materialized repo directory.

    Indexes repo_dir (which should be the source_dir, so expected_files
    paths line up), optionally embeds, then retrieves with top_k=30 and
    dedupes to file_k distinct files for metrics.
    """
    repo_path = Path(repo_dir)
    if db_path is None:
        import tempfile

        tmp = tempfile.mkdtemp()
        db_path = str(Path(tmp) / "index.sqlite")
        # Caller is expected to manage temp dir for external harness; we
        # just use a temp file here if not provided.
    else:
        db_path = str(db_path)

    start = time.perf_counter()
    reindex_index(db_path, str(repo_path))
    index_seconds = time.perf_counter() - start

    if provider is not None:
        run_embedding_worker(db_path, provider)

    # Load index for baseline computation and context packing
    index_result = load_index(db_path)
    retriever = build_hybrid_retriever(db_path, provider=provider, result=index_result)

    # Map relative_path -> document content for honest baseline
    docs_by_path = {d.relative_path: d for d in index_result.documents}
    symbols_by_key = {s.stable_key: s for s in index_result.symbols}

    results: list[ExternalQuestionResult] = []
    latencies: list[float] = []

    for q in questions:
        start = time.perf_counter()
        retrieval = retriever.retrieve(q.query, top_k=top_k)
        latency = time.perf_counter() - start
        latencies.append(latency)

        ranked_files = _dedupe_files(retrieval.candidates, limit=file_k)

        prec = _precision_at_k(q.expected_files, ranked_files, k=file_k)
        rec = recall_at_k(q.expected_files, ranked_files, k=file_k)
        rr = reciprocal_rank(q.expected_files, ranked_files)
        ceiling = _precision_ceiling_at_k(q.expected_files, k=file_k)
        normalized = (prec / ceiling) if ceiling > 0 else 0.0
        over_ret = _precision_over_returned(q.expected_files, ranked_files)

        # Honest token-savings: baseline is ground-truth files, actual is context pack
        baseline_texts: list[str] = []
        for fp in q.expected_files:
            doc = docs_by_path.get(fp)
            if doc is not None:
                baseline_texts.append(doc.content)
            else:
                # Fallback: read directly from repo_dir if not indexed (e.g., size-capped)
                try:
                    baseline_texts.append((repo_path / fp).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    pass
        baseline_tokens = estimate_tokens("\n".join(baseline_texts)) if baseline_texts else 0

        pack = build_context_pack(
            retrieval.candidates,
            query=q.query,
            graph=index_result.graph,
            symbols_by_key=symbols_by_key,
            token_budget=token_budget,
        )
        context_tokens = pack.total_tokens
        savings = token_reduction(context_tokens, baseline_tokens) if baseline_tokens else 0.0
        pack_files = pack.file_paths
        pack_recall = (
            len(q.expected_files & set(pack_files)) / len(q.expected_files)
            if q.expected_files
            else 0.0
        )

        results.append(
            ExternalQuestionResult(
                query=q.query,
                expected_files=q.expected_files,
                category=q.category,
                ranked_files=ranked_files,
                precision_at_10=prec,
                recall_at_10=rec,
                reciprocal_rank=rr,
                latency_seconds=latency,
                precision_ceiling_at_10=ceiling,
                precision_at_10_normalized=normalized,
                precision_over_returned=over_ret,
                baseline_tokens=baseline_tokens,
                context_tokens=context_tokens,
                savings_pct=savings,
                pack_files=pack_files,
                pack_recall=pack_recall,
            )
        )

    mean_p = mean(r.precision_at_10 for r in results)
    mean_r = mean(r.recall_at_10 for r in results)
    mean_rr = mean(r.reciprocal_rank for r in results)
    mean_ceiling = mean(r.precision_ceiling_at_10 for r in results)
    mean_norm = mean(r.precision_at_10_normalized for r in results)
    mean_over = mean(r.precision_over_returned for r in results)
    mean_baseline = mean(r.baseline_tokens for r in results)
    mean_context = mean(r.context_tokens for r in results)
    mean_savings = mean(r.savings_pct for r in results)
    mean_pack_recall = mean(r.pack_recall for r in results)
    aggregate_savings = token_reduction(mean_context, mean_baseline) if mean_baseline else 0.0
    p50 = statistics.median(latencies) if latencies else 0.0
    # p95 as 95th percentile
    p95 = 0.0
    if latencies:
        sorted_lat = sorted(latencies)
        idx = int(0.95 * len(sorted_lat))
        idx = min(idx, len(sorted_lat) - 1)
        p95 = sorted_lat[idx]

    return ExternalReport(
        questions=results,
        mean_precision_at_10=mean_p,
        mean_recall_at_10=mean_r,
        mean_reciprocal_rank=mean_rr,
        mean_precision_ceiling_at_10=mean_ceiling,
        mean_precision_at_10_normalized=mean_norm,
        mean_precision_over_returned=mean_over,
        p50_latency_seconds=p50,
        p95_latency_seconds=p95,
        index_seconds=index_seconds,
        total_questions=len(results),
        repo="",
        source_dir=str(repo_path),
        commit=None,
        mean_baseline_tokens=mean_baseline,
        mean_context_tokens=mean_context,
        mean_savings_pct=mean_savings,
        aggregate_savings_pct=aggregate_savings,
        mean_pack_recall=mean_pack_recall,
    )
