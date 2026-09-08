#!/usr/bin/env python3
"""Clone a real repo, index its source_dir, and run file-level retrieval evaluation.

Pin each repo to a commit SHA and record it in the output — cloning HEAD
makes numbers drift as the target repo changes underneath you.

Usage:
  python benchmarks/run_external.py --repo https://github.com/expressjs/express.git --source-dir lib --queries path/to/queries.json [--commit abc123] [--output benchmarks/results/express.json]
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure repo root is on sys.path for imports when run as script
ROOT = Path(__file__).resolve().parent.parent
# Unconditional: a stale editable-install copy of config.py (hatchling's
# force-include copies it into site-packages, which goes stale on every
# edit without a reinstall) can sit ahead of ROOT in sys.path even when
# ROOT is technically already present further back - `if ROOT not in
# sys.path` silently no-ops in that case and imports resolve to the
# stale copy instead. Insert at position 0 every time.
sys.path.insert(0, str(ROOT))

from evaluation.external import load_external_questions, run_external_evaluation


def _git(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def clone_repo(repo_url: str, dest: Path, commit: str | None = None) -> str:
    _git(["git", "clone", "--depth", "1", repo_url, str(dest)])
    if commit:
        # Fetch the specific commit if shallow clone didn't contain it
        try:
            _git(["git", "fetch", "--depth", "1", "origin", commit], cwd=dest)
            _git(["git", "checkout", commit], cwd=dest)
        except subprocess.CalledProcessError:
            # Fallback: fetch all and try again
            _git(["git", "fetch", "origin"], cwd=dest)
            _git(["git", "checkout", commit], cwd=dest)
        # Verify
        actual = _git(["git", "rev-parse", "HEAD"], cwd=dest)
        return actual
    else:
        actual = _git(["git", "rev-parse", "HEAD"], cwd=dest)
        return actual


def baseline_bucket(baseline_tokens: float) -> str:
    """Size bucket for a question by its baseline tokens.

    Buckets are the pre-registered headline unit (PREREGISTRATION.md): the
    whole-run mean mixes 96-token files with 3,800-token files, so report
    per bucket. Boundaries pinned by test_bucket_assignment_boundaries.
    """
    if baseline_tokens < 1000:
        return "<1k"
    if baseline_tokens <= 4000:
        return "1k-4k"
    return ">4k"


BUCKETS = ("<1k", "1k-4k", ">4k")


def _bucket_aggregates(questions: list[dict]) -> dict:
    """Aggregate savings + recall per baseline bucket.

    A bucket with no questions reports null fields, not 0.0 — 0.0 would
    read as "no savings" rather than "no data".
    """
    from evaluation.metrics import mean, token_reduction

    out: dict = {}
    for bucket in BUCKETS:
        members = [q for q in questions if q.get("baseline_bucket") == bucket]
        if not members:
            out[bucket] = {
                "count": 0,
                "mean_baseline_tokens": None,
                "mean_context_tokens": None,
                "aggregate_savings_pct": None,
                "mean_recall_at_10": None,
                "mean_pack_recall": None,
            }
            continue
        mean_baseline = mean(q["baseline_tokens"] for q in members)
        mean_context = mean(q["context_tokens"] for q in members)
        out[bucket] = {
            "count": len(members),
            "mean_baseline_tokens": mean_baseline,
            "mean_context_tokens": mean_context,
            "aggregate_savings_pct": token_reduction(mean_context, mean_baseline) if mean_baseline else 0.0,
            "mean_recall_at_10": mean(q["recall_at_10"] for q in members),
            # None, not 0.0, for reports written before pack_recall existed —
            # 0.0 would read as "the pack contained nothing".
            "mean_pack_recall": (
                mean(q["pack_recall"] for q in members)
                if all("pack_recall" in q for q in members)
                else None
            ),
        }
    return out


def _recompute_file(path: Path) -> None:
    """Recompute ceiling-aware metrics for an existing report in-place."""
    from evaluation.external import (
        _precision_at_k,
        _precision_ceiling_at_k,
        _precision_over_returned,
    )
    from evaluation.metrics import mean, recall_at_k, reciprocal_rank, token_reduction

    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    for q in questions:
        expected = frozenset(q.get("expected_files", []))
        ranked = q.get("ranked_files", [])

        # Recompute and verify original metrics match exactly
        exp_prec = _precision_at_k(expected, ranked, k=10)
        exp_rec = recall_at_k(expected, ranked, 10)
        exp_rr = reciprocal_rank(expected, ranked)

        stored_prec = q.get("precision_at_10")
        stored_rec = q.get("recall_at_10")
        stored_rr = q.get("reciprocal_rank")

        # Strict equality check — proof recompute is faithful
        if stored_prec is not None and exp_prec != stored_prec:
            raise ValueError(f"{path}: precision_at_10 mismatch for query {q.get('query')!r}: stored {stored_prec} != recomputed {exp_prec}")
        if stored_rec is not None and exp_rec != stored_rec:
            raise ValueError(f"{path}: recall_at_10 mismatch for query {q.get('query')!r}: stored {stored_rec} != recomputed {exp_rec}")
        if stored_rr is not None and exp_rr != stored_rr:
            raise ValueError(f"{path}: reciprocal_rank mismatch for query {q.get('query')!r}: stored {stored_rr} != recomputed {exp_rr}")

        # Backfill when a hand-written report lacks a stored metric; the
        # strict checks above still fire whenever a stored value exists.
        q.setdefault("precision_at_10", exp_prec)
        q.setdefault("recall_at_10", exp_rec)
        q.setdefault("reciprocal_rank", exp_rr)

        ceiling = _precision_ceiling_at_k(expected, k=10)
        normalized = (exp_prec / ceiling) if ceiling > 0 else 0.0
        over_ret = _precision_over_returned(expected, ranked)

        q["precision_ceiling_at_10"] = ceiling
        q["precision_at_10_normalized"] = normalized
        q["precision_over_returned"] = over_ret

    # Aggregate means for new fields
    mean_prec = mean(q["precision_at_10"] for q in questions) if questions else 0.0
    mean_ceiling = mean(q["precision_ceiling_at_10"] for q in questions) if questions else 0.0
    mean_norm = mean(q["precision_at_10_normalized"] for q in questions) if questions else 0.0
    mean_over = mean(q["precision_over_returned"] for q in questions) if questions else 0.0

    # Overwrite means to ensure recomputed values (should match original for prec)
    data["mean_precision_at_10"] = mean_prec
    data["mean_precision_ceiling_at_10"] = mean_ceiling
    data["mean_precision_at_10_normalized"] = mean_norm
    data["mean_precision_over_returned"] = mean_over
    # mean_recall and mean_rr should already match recomputed, but recompute for consistency
    data["mean_recall_at_10"] = mean(q["recall_at_10"] for q in questions) if questions else 0.0
    data["mean_reciprocal_rank"] = mean(q["reciprocal_rank"] for q in questions) if questions else 0.0

    # Aggregate (token-volume-weighted) savings — see evaluation/external.py
    # ExternalReport.aggregate_savings_pct docstring for why this, not
    # mean_savings_pct, is the number to cite.
    if "mean_baseline_tokens" in data and "mean_context_tokens" in data:
        data["aggregate_savings_pct"] = token_reduction(data["mean_context_tokens"], data["mean_baseline_tokens"])

    # Backfill baseline buckets + per-bucket aggregates for reports that
    # predate them (bucket missing or reports without per-question tokens).
    needs_buckets = any("baseline_bucket" not in q for q in questions)
    if needs_buckets and all("baseline_tokens" in q and "context_tokens" in q for q in questions):
        for q in questions:
            q["baseline_bucket"] = baseline_bucket(q.get("baseline_tokens", 0))
    if any("baseline_bucket" in q for q in questions):
        data["buckets"] = _bucket_aggregates(questions)

    # Per-budget blocks (multi-budget runs): recompute each block's
    # aggregates and buckets the same way, keeping strict-equality checks above.
    for block in data.get("budgets", {}).values():
        bq = block.get("questions", [])
        if all("baseline_tokens" in q and "context_tokens" in q for q in bq):
            for q in bq:
                q.setdefault("baseline_bucket", baseline_bucket(q.get("baseline_tokens", 0)))
            block["buckets"] = _bucket_aggregates(bq)
        if "mean_baseline_tokens" in block and "mean_context_tokens" in block:
            block["aggregate_savings_pct"] = token_reduction(
                block["mean_context_tokens"], block["mean_baseline_tokens"]
            )

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Recomputed {path}: P@10 {mean_prec:.3f} ceiling {mean_ceiling:.3f} norm {mean_norm:.3f} over_ret {mean_over:.3f}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run external file-level benchmark")
    parser.add_argument("--repo", required=False, help="Clone URL")
    parser.add_argument("--source-dir", required=False, help="Source directory relative to repo root (e.g., lib, fastapi, .)")
    parser.add_argument("--queries", required=False, help="Path to query JSON — list of {query, expected_files}")
    parser.add_argument("--commit", default=None, help="Commit SHA to pin (default: HEAD)")
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--no-embed", action="store_true", help="Disable vector search (default: enabled)")
    parser.add_argument("--token-budget", type=int, default=800, help="Context-pack token budget for the run (default: 800)")
    parser.add_argument("--budgets", default=None, help="Comma-separated budgets to run, e.g. 800,1200,2000 (overrides --token-budget; results nested under 'budgets')")
    parser.add_argument("--recompute", default=None, help="Recompute ceiling-aware metrics for existing report(s) in-place")
    args = parser.parse_args()

    if args.recompute:
        # Support single file or glob; allow multiple via comma
        import glob

        paths: list[Path] = []
        # Handle comma-separated or glob
        for part in args.recompute.split(","):
            part = part.strip()
            if not part:
                continue
            for p in glob.glob(part):
                paths.append(Path(p))
            if not paths and Path(part).exists():
                paths.append(Path(part))
        if not paths:
            # Treat as single path without glob
            paths = [Path(args.recompute)]
        for p in paths:
            _recompute_file(p)
        return 0

    # Normal run requires repo/source-dir/queries
    if not args.repo or not args.source_dir or not args.queries:
        parser.error("--repo, --source-dir, and --queries are required unless --recompute is used")

    repo_url = args.repo
    source_dir = args.source_dir
    queries_path = args.queries
    commit = args.commit
    output = args.output
    if args.budgets:
        budgets = [int(b.strip()) for b in args.budgets.split(",") if b.strip()]
    else:
        budgets = [args.token_budget]

    questions = load_external_questions(queries_path)
    print(f"Loaded {len(questions)} queries from {queries_path}", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dest = tmp_path / "repo"
        print(f"Cloning {repo_url} ...", file=sys.stderr)
        actual_commit = clone_repo(repo_url, clone_dest, commit=commit)
        print(f"Cloned at {actual_commit}", file=sys.stderr)

        # Resolve source_dir relative to clone
        if source_dir == ".":
            repo_dir = clone_dest
        else:
            repo_dir = clone_dest / source_dir
            if not repo_dir.exists():
                print(f"source_dir {source_dir} does not exist in {clone_dest}", file=sys.stderr)
                return 1

        # Use a DB inside tmp
        db_path = str(tmp_path / "index.sqlite")

        provider = None
        if not args.no_embed:
            try:
                from embeddings.local_provider import LocalEmbeddingProvider

                print("Loading embedding model all-MiniLM-L6-v2 ...", file=sys.stderr)
                provider = LocalEmbeddingProvider()
            except Exception as e:
                print(f"Failed to load embedding model: {e}", file=sys.stderr)
                provider = None

        def _serialize(report) -> dict:
            qdicts = []
            for r in report.questions:
                bucket = baseline_bucket(r.baseline_tokens)
                qdicts.append(
                    {
                        "query": r.query,
                        "expected_files": sorted(r.expected_files),
                        "ranked_files": r.ranked_files,
                        "precision_at_10": r.precision_at_10,
                        "precision_ceiling_at_10": r.precision_ceiling_at_10,
                        "precision_at_10_normalized": r.precision_at_10_normalized,
                        "precision_over_returned": r.precision_over_returned,
                        "recall_at_10": r.recall_at_10,
                        "reciprocal_rank": r.reciprocal_rank,
                        "latency_seconds": r.latency_seconds,
                        "category": r.category,
                        "baseline_tokens": r.baseline_tokens,
                        "context_tokens": r.context_tokens,
                        "savings_pct": r.savings_pct,
                        "pack_files": list(r.pack_files),
                        "pack_recall": r.pack_recall,
                        "baseline_bucket": bucket,
                    }
                )
            return {
                "total_questions": report.total_questions,
                "mean_precision_at_10": report.mean_precision_at_10,
                "mean_precision_ceiling_at_10": report.mean_precision_ceiling_at_10,
                "mean_precision_at_10_normalized": report.mean_precision_at_10_normalized,
                "mean_precision_over_returned": report.mean_precision_over_returned,
                "mean_recall_at_10": report.mean_recall_at_10,
                "mean_reciprocal_rank": report.mean_reciprocal_rank,
                "p50_latency_seconds": report.p50_latency_seconds,
                "p95_latency_seconds": report.p95_latency_seconds,
                "index_seconds": report.index_seconds,
                "mean_baseline_tokens": report.mean_baseline_tokens,
                "mean_context_tokens": report.mean_context_tokens,
                "mean_savings_pct": report.mean_savings_pct,
                "aggregate_savings_pct": report.aggregate_savings_pct,
                "mean_pack_recall": report.mean_pack_recall,
                "buckets": _bucket_aggregates(qdicts),
                "questions": qdicts,
            }

        budget_blocks: dict = {}
        for budget in budgets:
            print(f"Indexing {repo_dir} (budget {budget}) ...", file=sys.stderr)
            report = run_external_evaluation(
                repo_dir,
                questions,
                provider=provider,
                db_path=db_path,
                token_budget=budget,
            )
            report.repo = repo_url
            report.source_dir = source_dir
            report.commit = actual_commit
            budget_blocks[str(budget)] = _serialize(report)

        # Flat top-level keys stay for back-compat readers (first budget).
        primary = budget_blocks[str(budgets[0])]
        # Build output dict
        output_data = {
            "repo": repo_url,
            "source_dir": source_dir,
            "commit": actual_commit,
            "queries_file": str(queries_path),
            "token_budget": budgets[0],
            "token_budgets": budgets,
            **primary,
        }
        if len(budgets) > 1:
            output_data["budgets"] = budget_blocks

        # Print summary to stderr
        for budget in budgets:
            block = budget_blocks[str(budget)]
            print(f"\nResults for {repo_url} @ {actual_commit[:8]} ({source_dir}) budget {budget}", file=sys.stderr)
            print(f"  Questions: {block['total_questions']}", file=sys.stderr)
            print(f"  P@10: {block['mean_precision_at_10']:.3f}", file=sys.stderr)
            print(f"  R@10 (ranked list, pre-budget): {block['mean_recall_at_10']:.3f}", file=sys.stderr)
            print(f"  Pack recall (post-budget): {block['mean_pack_recall']:.3f}", file=sys.stderr)
            print(f"  MRR: {block['mean_reciprocal_rank']:.3f}", file=sys.stderr)
            print(f"  p50 latency: {block['p50_latency_seconds']*1000:.1f} ms", file=sys.stderr)
            print(f"  p95 latency: {block['p95_latency_seconds']*1000:.1f} ms", file=sys.stderr)
            print(f"  Index time: {block['index_seconds']:.1f} s", file=sys.stderr)
            print(f"  Aggregate savings: {block['aggregate_savings_pct']*100:.1f}% (baseline {block['mean_baseline_tokens']:.0f} -> context {block['mean_context_tokens']:.0f})", file=sys.stderr)

        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(output_data, indent=2), encoding="utf-8")
            print(f"Wrote {output}", file=sys.stderr)
        else:
            print(json.dumps(output_data, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
