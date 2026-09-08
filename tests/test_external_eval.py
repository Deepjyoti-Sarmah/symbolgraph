import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evaluation.external import (
    _dedupe_files,
    _precision_at_k,
    _precision_ceiling_at_k,
    _precision_over_returned,
    load_external_questions,
)
from evaluation.metrics import recall_at_k, reciprocal_rank


class TestExternalScorer(unittest.TestCase):
    def test_dedupe_keeps_first_occurrence_and_respects_limit(self):
        # Synthetic candidates with duplicate file paths
        candidates = [
            SimpleNamespace(relative_path="a.py"),
            SimpleNamespace(relative_path="b.py"),
            SimpleNamespace(relative_path="a.py"),  # duplicate, should be ignored
            SimpleNamespace(relative_path="c.py"),
            SimpleNamespace(relative_path="b.py"),  # duplicate
            SimpleNamespace(relative_path="d.py"),
        ]
        result = _dedupe_files(candidates, limit=10)
        self.assertEqual(result, ["a.py", "b.py", "c.py", "d.py"])

        # Respects limit: only first 2 distinct
        limited = _dedupe_files(candidates, limit=2)
        self.assertEqual(limited, ["a.py", "b.py"])

        # Keeps first occurrence order
        candidates2 = [
            SimpleNamespace(relative_path="x.py"),
            SimpleNamespace(relative_path="y.py"),
            SimpleNamespace(relative_path="z.py"),
        ]
        self.assertEqual(_dedupe_files(candidates2, limit=2), ["x.py", "y.py"])

    def test_precision_at_k_fixed_denominator(self):
        # Pin current behavior: divides by fixed k=10, not by returned count
        expected = frozenset({"a.py", "b.py"})
        ranked = ["a.py", "c.py", "d.py"]  # 1 hit
        # hits=1, k=10 => 0.1 even though only 3 returned
        self.assertAlmostEqual(_precision_at_k(expected, ranked, k=10), 0.1)
        # With k=5, 1 hit => 0.2
        self.assertAlmostEqual(_precision_at_k(expected, ranked, k=5), 0.2)
        # Empty expected => 0.0
        self.assertEqual(_precision_at_k(frozenset(), ranked, k=10), 0.0)
        # k=0 => 0.0
        self.assertEqual(_precision_at_k(expected, ranked, k=0), 0.0)

        # Express lib small-repo case: only 2 distinct files returned, 1 hit, still 1/10
        expected_small = frozenset({"application.js"})
        ranked_small = ["express.js", "application.js"]  # 1 hit, 2 returned, but fixed k=10 => 0.1
        self.assertAlmostEqual(_precision_at_k(expected_small, ranked_small, k=10), 0.1)

    def test_ceiling_and_normalized(self):
        # Ceiling is min(|expected|, k)/k
        self.assertAlmostEqual(_precision_ceiling_at_k(frozenset({"a.py"}), k=10), 0.1)
        self.assertAlmostEqual(_precision_ceiling_at_k(frozenset({"a.py", "b.py"}), k=10), 0.2)
        self.assertAlmostEqual(_precision_ceiling_at_k(frozenset({"a.py", "b.py", "c.py"}), k=2), 1.0)
        self.assertAlmostEqual(_precision_ceiling_at_k(frozenset(), k=10), 0.0)
        self.assertEqual(_precision_ceiling_at_k(frozenset({"a.py"}), k=0), 0.0)

        # Normalized is 1.0 when all expected retrieved within k
        expected = frozenset({"a.py", "b.py"})
        ranked = ["a.py", "b.py", "c.py"]
        prec = _precision_at_k(expected, ranked, k=10)  # 2/10=0.2
        ceiling = _precision_ceiling_at_k(expected, k=10)  # 0.2
        normalized = (prec / ceiling) if ceiling else 0.0
        self.assertAlmostEqual(normalized, 1.0)

        # Partial retrieval: 1 of 2 => prec 0.1, ceiling 0.2 => normalized 0.5
        ranked_partial = ["a.py", "c.py"]
        prec2 = _precision_at_k(expected, ranked_partial, k=10)
        normalized2 = (prec2 / ceiling) if ceiling else 0.0
        self.assertAlmostEqual(normalized2, 0.5)

        # Ceiling 0 => normalized 0.0 (not division by zero)
        empty_expected = frozenset()
        prec_empty = _precision_at_k(empty_expected, ranked, k=10)
        ceiling_empty = _precision_ceiling_at_k(empty_expected, k=10)
        normalized_empty = (prec_empty / ceiling_empty) if ceiling_empty else 0.0
        self.assertEqual(normalized_empty, 0.0)

    def test_precision_over_returned(self):
        # Divides by returned count, not fixed k
        expected = frozenset({"a.py", "b.py"})
        ranked = ["a.py", "c.py", "d.py"]  # 1 hit, 3 returned => 0.333...
        self.assertAlmostEqual(_precision_over_returned(expected, ranked), 1 / 3)

        # Empty ranked => 0.0, not ZeroDivisionError
        self.assertEqual(_precision_over_returned(expected, []), 0.0)
        # Also empty ranked with empty expected => 0.0
        self.assertEqual(_precision_over_returned(frozenset(), []), 0.0)

        # Full hit: 2 expected, 2 returned both hits => 1.0
        ranked_full = ["a.py", "b.py"]
        self.assertAlmostEqual(_precision_over_returned(expected, ranked_full), 1.0)

        # if only 2 distinct files returned, precision is hits/2
        expected_small = frozenset({"application.js"})
        ranked_small = ["express.js", "application.js"]
        self.assertAlmostEqual(_precision_over_returned(expected_small, ranked_small), 0.5)
        # Fixed-k would give 0.1, over_returned gives 0.5 — different denominator

    def test_recall_and_mrr_known_ranking(self):
        expected = frozenset({"a.py", "b.py", "c.py"})
        # Ranked: b at pos1, a at pos2, d at pos3, c at pos4
        ranked = ["b.py", "a.py", "d.py", "c.py"]
        # Recall@2: hits in top2 = b,a => 2/3
        self.assertAlmostEqual(recall_at_k(expected, ranked, k=2), 2 / 3)
        # Recall@10: all 3 in top10 => 1.0
        self.assertAlmostEqual(recall_at_k(expected, ranked, k=10), 1.0)
        # MRR: first hit b at rank1 => 1.0
        self.assertAlmostEqual(reciprocal_rank(expected, ranked), 1.0)

        # Another ranking: first hit at rank3
        ranked2 = ["x.py", "y.py", "a.py", "b.py"]
        self.assertAlmostEqual(reciprocal_rank(expected, ranked2), 1 / 3)
        # No hit => 0.0
        ranked_none = ["x.py", "y.py"]
        self.assertEqual(reciprocal_rank(expected, ranked_none), 0.0)
        self.assertEqual(recall_at_k(expected, ranked_none, k=10), 0.0)

    def test_load_external_questions_parses_query_expected_files_shape(self):
        data = [
            {"query": "How does X work?", "expected_files": ["a.py", "b.py"], "category": "core"},
            {"query": "Another?", "expected_files": ["c.py"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            questions = load_external_questions(path)
            self.assertEqual(len(questions), 2)
            self.assertEqual(questions[0].query, "How does X work?")
            self.assertEqual(questions[0].expected_files, frozenset({"a.py", "b.py"}))
            self.assertEqual(questions[0].category, "core")
            self.assertEqual(questions[1].query, "Another?")
            self.assertEqual(questions[1].expected_files, frozenset({"c.py"}))
            self.assertEqual(questions[1].category, "")

    def test_load_external_questions_empty_expected(self):
        data = [{"query": "Q?", "expected_files": [], "category": "test"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            questions = load_external_questions(path)
            self.assertEqual(questions[0].expected_files, frozenset())

    def test_honest_savings_uses_ground_truth_files_as_baseline(self):
        # Baseline must be ground-truth files, not whole repo or retrieved files.
        # Create a tiny repo with two files, one is ground truth.
        import tempfile

        from evaluation.external import ExternalQuestion, run_external_evaluation
        from retrieval.context_builder import estimate_tokens

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # Ground-truth file (what agent must read to answer)
            (repo / "expected.py").write_text("x = 1\n" * 100, encoding="utf-8")
            # Unrelated large file (whole-repo would inflate baseline)
            (repo / "unrelated.py").write_text("y = 2\n" * 1000, encoding="utf-8")
            # Another file that retrieval might actually return
            (repo / "other.py").write_text("def foo(): pass\n", encoding="utf-8")

            q = ExternalQuestion(query="what is x", expected_files=frozenset({"expected.py"}))
            report = run_external_evaluation(repo, [q], provider=None, top_k=5, file_k=10)

            self.assertEqual(len(report.questions), 1)
            qr = report.questions[0]
            # Baseline should be tokens of expected.py only, not whole repo
            expected_baseline = estimate_tokens((repo / "expected.py").read_text(encoding="utf-8"))
            self.assertEqual(qr.baseline_tokens, expected_baseline)
            # Whole-repo baseline would be much larger
            whole_repo_tokens = estimate_tokens(
                (repo / "expected.py").read_text(encoding="utf-8")
                + "\n"
                + (repo / "unrelated.py").read_text(encoding="utf-8")
                + "\n"
                + (repo / "other.py").read_text(encoding="utf-8")
            )
            self.assertLess(qr.baseline_tokens, whole_repo_tokens)
            # Savings is paired with recall: must exist alongside recall
            self.assertIsInstance(qr.recall_at_10, float)
            self.assertIsInstance(qr.savings_pct, float)
            # Savings should be 1 - context/baseline, not derived from retrieved set size
            if qr.baseline_tokens > 0:
                self.assertAlmostEqual(qr.savings_pct, 1.0 - qr.context_tokens / qr.baseline_tokens, delta=1e-6)

    def test_savings_not_circular_with_retrieval(self):
        # Retrieving worse should not improve savings, because baseline is fixed
        from evaluation.external import ExternalQuestion, run_external_evaluation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def a(): pass\n", encoding="utf-8")
            (repo / "b.py").write_text("def b(): pass\n", encoding="utf-8")

            q = ExternalQuestion(query="a", expected_files=frozenset({"a.py"}))
            report = run_external_evaluation(repo, [q], provider=None, top_k=5, file_k=10)
            qr = report.questions[0]
            # Baseline independent of what was retrieved
            self.assertIn("a.py", q.expected_files)
            self.assertGreater(qr.baseline_tokens, 0)
            # Report includes both recall and savings in same row
            self.assertTrue(hasattr(qr, "recall_at_10"))
            self.assertTrue(hasattr(qr, "savings_pct"))
            # Mean aggregates also paired
            self.assertTrue(hasattr(report, "mean_recall_at_10"))
            self.assertTrue(hasattr(report, "mean_savings_pct"))

    def test_multi_budget_monotonic(self):
        # Across budgets 800/1200/2000, context_tokens must be
        # non-decreasing and aggregate_savings_pct non-increasing. A budget
        # flag that silently isn't applied would show identical contexts.
        from evaluation.external import ExternalQuestion, run_external_evaluation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # Enough matchable symbols that every budget binds: ~150
            # functions, all containing the query term.
            body = "\n".join(
                f'def fn_{i}():\n    """Function number {i} returns its index."""\n    return {i}\n'
                for i in range(150)
            )
            (repo / "big.py").write_text(body, encoding="utf-8")

            q = ExternalQuestion(query="return", expected_files=frozenset({"big.py"}))
            contexts = []
            aggregates = []
            for budget in (800, 1200, 2000):
                report = run_external_evaluation(
                    repo, [q], provider=None, top_k=30, file_k=10, token_budget=budget
                )
                contexts.append(report.mean_context_tokens)
                aggregates.append(report.aggregate_savings_pct)

            self.assertLessEqual(contexts[0], contexts[1])
            self.assertLessEqual(contexts[1], contexts[2])
            # Budgets must actually bind — otherwise this test is vacuous.
            self.assertLess(contexts[0], contexts[2])
            self.assertGreaterEqual(aggregates[0], aggregates[1])
            self.assertGreaterEqual(aggregates[1], aggregates[2])
            self.assertGreater(aggregates[0], aggregates[2])

    def test_pack_recall_catches_truncation_that_recall_at_10_cannot(self):
        # The savings claim is made on the context pack, but recall_at_10 is
        # scored on `ranked_files`, which is built before the token budget is
        # applied. So a budget small enough to drop every definition still
        # reports perfect recall next to a near-perfect saving. pack_recall
        # scores the pack itself and is what falls.
        from evaluation.external import ExternalQuestion, run_external_evaluation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            body = "\n".join(
                f'def handler_{i}():\n    """Handler {i} processes the request payload."""\n    return {i}\n'
                for i in range(60)
            )
            (repo / "expected.py").write_text(body, encoding="utf-8")

            q = ExternalQuestion(
                query="handler processes request", expected_files=frozenset({"expected.py"})
            )
            # A budget too small for even one definition to fit.
            report = run_external_evaluation(
                repo, [q], provider=None, top_k=30, file_k=10, token_budget=5
            )
            qr = report.questions[0]

            # The file is retrieved, so the pre-budget metric is perfect...
            self.assertEqual(qr.recall_at_10, 1.0)
            # ...and the saving looks excellent...
            self.assertGreater(qr.savings_pct, 0.9)
            # ...but nothing survived the budget, so the pack is empty and the
            # "saving" bought an agent nothing.
            self.assertEqual(qr.pack_files, ())
            self.assertEqual(qr.pack_recall, 0.0)
            self.assertEqual(report.mean_pack_recall, 0.0)

    def test_pack_recall_responds_to_budget_while_recall_at_10_does_not(self):
        # The divergence that motivates the metric: across budgets, the
        # ranked-list recall is constant by construction (the budget is applied
        # after ranking), so a recall gate on it cannot fail no matter how hard
        # the pack is truncated. pack_recall is monotonic in the budget.
        from evaluation.external import ExternalQuestion, run_external_evaluation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            body = "\n".join(
                f'def handler_{i}():\n    """Handler {i} processes the request payload."""\n    return {i}\n'
                for i in range(60)
            )
            (repo / "expected.py").write_text(body, encoding="utf-8")

            q = ExternalQuestion(
                query="handler processes request", expected_files=frozenset({"expected.py"})
            )
            list_recalls = []
            pack_recalls = []
            for budget in (5, 800):
                report = run_external_evaluation(
                    repo, [q], provider=None, top_k=30, file_k=10, token_budget=budget
                )
                list_recalls.append(report.mean_recall_at_10)
                pack_recalls.append(report.mean_pack_recall)

            # Unchanged across a 160x budget cut — this is the blind spot.
            self.assertEqual(list_recalls[0], list_recalls[1])
            # pack_recall separates the two runs.
            self.assertLess(pack_recalls[0], pack_recalls[1])
            self.assertEqual(pack_recalls[1], 1.0)

    def test_bucket_assignment_boundaries(self):
        # Pre-registered buckets: <1k, 1k-4k, >4k by baseline_tokens.
        from benchmarks.run_external import _bucket_aggregates, baseline_bucket

        self.assertEqual(baseline_bucket(0), "<1k")
        self.assertEqual(baseline_bucket(999), "<1k")
        self.assertEqual(baseline_bucket(1000), "1k-4k")
        self.assertEqual(baseline_bucket(4000), "1k-4k")
        self.assertEqual(baseline_bucket(4001), ">4k")

        # A bucket with no questions reports null, not 0.0 (0.0 would read
        # as "no savings" rather than "no data").
        questions = [
            {"baseline_tokens": 500, "context_tokens": 800, "recall_at_10": 1.0,
             "baseline_bucket": "<1k"},
            {"baseline_tokens": 3000, "context_tokens": 800, "recall_at_10": 1.0,
             "baseline_bucket": "1k-4k"},
        ]
        buckets = _bucket_aggregates(questions)
        self.assertEqual(buckets["<1k"]["count"], 1)
        self.assertEqual(buckets["1k-4k"]["count"], 1)
        self.assertEqual(buckets[">4k"]["count"], 0)
        self.assertIsNone(buckets[">4k"]["aggregate_savings_pct"])
        self.assertIsNone(buckets[">4k"]["mean_baseline_tokens"])
        self.assertIsNone(buckets[">4k"]["mean_context_tokens"])
        self.assertIsNone(buckets[">4k"]["mean_recall_at_10"])
        # Populated bucket is token-volume-weighted like the top-level number.
        self.assertAlmostEqual(
            buckets["1k-4k"]["aggregate_savings_pct"], 1.0 - 800 / 3000, delta=1e-9
        )

    def test_recompute_strict_equality(self):
        # _recompute_file must never quietly restate a stored metric:
        # tampered precision/recall/RR raises; faithful files pass and gain
        # buckets + aggregate backfill.
        from benchmarks.run_external import _recompute_file

        def _report(precision=0.1):
            return {
                "mean_baseline_tokens": 3000,
                "mean_context_tokens": 800,
                "questions": [
                    {
                        "query": "q",
                        "expected_files": ["a.py"],
                        "ranked_files": ["a.py", "b.py"],
                        "precision_at_10": precision,
                        "recall_at_10": 1.0,
                        "reciprocal_rank": 1.0,
                        "baseline_tokens": 3000,
                        "context_tokens": 800,
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            faithful = Path(tmp) / "faithful.json"
            faithful.write_text(json.dumps(_report()), encoding="utf-8")
            _recompute_file(faithful)  # must not raise
            data = json.loads(faithful.read_text(encoding="utf-8"))
            self.assertEqual(data["questions"][0]["baseline_bucket"], "1k-4k")
            self.assertAlmostEqual(
                data["aggregate_savings_pct"], 1.0 - 800 / 3000, delta=1e-9
            )
            self.assertEqual(data["buckets"]["1k-4k"]["count"], 1)

            tampered = Path(tmp) / "tampered.json"
            tampered.write_text(json.dumps(_report(precision=0.5)), encoding="utf-8")
            with self.assertRaises(ValueError):
                _recompute_file(tampered)

    def test_aggregate_savings_is_token_weighted_not_mean_of_ratios(self):
        # A handful of tiny files (context-pack overhead exceeds the file's
        # own size) alongside one large file should not let mean_savings_pct
        # (mean of per-query ratios) drag a token-weighted claim negative.
        # This is the exact skew a self-authored benchmark against
        # retrieval/ hit in practice: mean_savings_pct came back -1.68
        # while the set saved 16.7% of tokens in aggregate.
        from evaluation.external import ExternalQuestion, run_external_evaluation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # Three tiny files: context-pack overhead will exceed their size.
            for name in ("tiny_a.py", "tiny_b.py", "tiny_c.py"):
                (repo / name).write_text("def f(): pass\n", encoding="utf-8")
            # One large file: real savings should show up here.
            (repo / "big.py").write_text(
                "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(200)),
                encoding="utf-8",
            )

            questions = [
                ExternalQuestion(query=name.split(".")[0], expected_files=frozenset({name}))
                for name in ("tiny_a.py", "tiny_b.py", "tiny_c.py", "big.py")
            ]
            report = run_external_evaluation(repo, questions, provider=None, top_k=5, file_k=10)

            self.assertTrue(hasattr(report, "aggregate_savings_pct"))
            # The token-weighted number must match summing baseline/context
            # across all queries and taking one ratio — not a bare average
            # of report.mean_savings_pct-style per-query ratios.
            total_baseline = sum(q.baseline_tokens for q in report.questions)
            total_context = sum(q.context_tokens for q in report.questions)
            expected_aggregate = 1.0 - (total_context / total_baseline)
            self.assertAlmostEqual(report.aggregate_savings_pct, expected_aggregate, delta=1e-6)
            # The two statistics must be able to disagree in sign — that's
            # the whole reason aggregate_savings_pct exists.
            if report.mean_savings_pct < 0:
                self.assertGreater(report.aggregate_savings_pct, report.mean_savings_pct)


if __name__ == "__main__":
    unittest.main()
