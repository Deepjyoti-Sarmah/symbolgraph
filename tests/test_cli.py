import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from embeddings.fake_provider import FakeEmbeddingProvider
from symbolgraph.cli import (
    build_parser,
    cmd_callees,
    cmd_callers,
    cmd_context,
    cmd_definition,
    cmd_imports,
    cmd_index,
    cmd_init,
    cmd_search,
    cmd_status,
    default_db_path,
    has_embeddings,
    main,
)

AUTH = {
    "auth.ts": (
        "export function createAuth() { return 1; }\n"
        "export function login(name: string) { createAuth(); return name; }\n"
    ),
    "api.ts": (
        'import { login } from "./auth";\nexport function run() { login("admin"); }\n'
    ),
}


def _write(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        (root / relative_path).write_text(content, encoding="utf-8")


class TestCliCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = str(self.root / "index.sqlite")
        _write(self.root, AUTH)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_db_path_is_dot_ckg_under_root(self):
        self.assertEqual(
            default_db_path(str(self.root)),
            str(self.root / ".sg" / "index.sqlite"),
        )

    def test_index_creates_db_and_reports_parsed_files(self):
        report = cmd_index(str(self.root), self.db_path)

        self.assertTrue(Path(self.db_path).exists())
        self.assertEqual(report.parsed_files, 2)

    def test_index_without_provider_leaves_embeddings_empty(self):
        cmd_index(str(self.root), self.db_path)

        self.assertFalse(has_embeddings(self.db_path))

    def test_index_with_provider_embeds_chunks(self):
        cmd_index(
            str(self.root), self.db_path, provider=FakeEmbeddingProvider(dimension=8)
        )

        self.assertTrue(has_embeddings(self.db_path))

    def test_status_reports_generation_and_counts(self):
        cmd_index(str(self.root), self.db_path)

        status = cmd_status(self.db_path)

        self.assertEqual(status["generation"], 1)
        self.assertEqual(status["documents"], 2)
        self.assertGreater(status["symbols"], 0)
        self.assertGreater(status["chunks"], 0)
        self.assertEqual(status["embeddings"], 0)
        self.assertEqual(status["embedding_jobs"]["PENDING"], status["chunks"])

    def test_search_without_provider_uses_fts_and_exact(self):
        cmd_index(str(self.root), self.db_path)

        retrieval = cmd_search(self.db_path, "login")

        self.assertIn("login", {c.symbol_name for c in retrieval.candidates})

    def test_search_with_provider_uses_vector_source(self):
        provider = FakeEmbeddingProvider(dimension=8)
        cmd_index(str(self.root), self.db_path, provider=provider)

        retrieval = cmd_search(self.db_path, "authentication flow", provider=provider)

        sources = {source for c in retrieval.candidates for source in c.sources}
        self.assertIn("vector", sources)

    def test_definition_finds_symbol(self):
        cmd_index(str(self.root), self.db_path)

        retrieval = cmd_definition(self.db_path, "createAuth")

        self.assertEqual(retrieval.strategy, "exact_symbol")
        self.assertEqual({c.symbol_name for c in retrieval.candidates}, {"createAuth"})

    def test_callers_and_callees(self):
        cmd_index(str(self.root), self.db_path)

        callers = cmd_callers(self.db_path, "login")
        callees = cmd_callees(self.db_path, "login")

        self.assertEqual({c.symbol_name for c in callers.candidates}, {"run"})
        self.assertEqual({c.symbol_name for c in callees.candidates}, {"createAuth"})

    def test_imports_lists_resolved_and_local_names(self):
        cmd_index(str(self.root), self.db_path)

        imports = cmd_imports(self.db_path, "api.ts")

        self.assertEqual(len(imports), 1)
        import_reference, resolved = imports[0]
        self.assertEqual(import_reference.local_name, "login")
        assert resolved is not None
        assert resolved.target_symbol is not None
        self.assertEqual(resolved.target_document.relative_path, "auth.ts")
        self.assertEqual(resolved.target_symbol.name, "login")

    def test_imports_on_unknown_file_is_empty(self):
        cmd_index(str(self.root), self.db_path)

        self.assertEqual(cmd_imports(self.db_path, "missing.ts"), [])

    def test_context_returns_pack_within_budget(self):
        cmd_index(str(self.root), self.db_path)

        pack = cmd_context(self.db_path, "login", token_budget=500)

        self.assertLessEqual(pack.total_tokens, 500)
        self.assertIn(
            "login",
            [entry.qualified_name for entry in pack.primary_definitions],
        )


class TestCliMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = str(self.root / "index.sqlite")
        _write(self.root, AUTH)

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_command_creates_db(self):
        exit_code = main(["--db", self.db_path, "index", str(self.root)])

        self.assertEqual(exit_code, 0)
        self.assertTrue(Path(self.db_path).exists())

    def test_status_before_index_reports_missing_and_nonzero_exit(self):
        exit_code = main(["--db", self.db_path, "status"])

        self.assertEqual(exit_code, 1)

    def test_status_after_index_succeeds(self):
        main(["--db", self.db_path, "index", str(self.root)])

        exit_code = main(["--db", self.db_path, "status"])

        self.assertEqual(exit_code, 0)

    def test_search_command_defaults_to_no_vector_without_embeddings(self):
        main(["--db", self.db_path, "index", str(self.root)])

        exit_code = main(["--db", self.db_path, "search", "login"])

        self.assertEqual(exit_code, 0)

    def test_eval_command_runs_without_touching_the_target_db(self):
        # eval runs entirely against its own copy of the fixed benchmark
        # repo; it takes no --db/path and must not require a prior index.
        exit_code = main(["eval"])

        self.assertEqual(exit_code, 0)

    def test_version_prints_and_exits_zero(self):
        parser = build_parser()
        # argparse version action exits via SystemExit(0) and prints version
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
            parser.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        output = stdout.getvalue() + stderr.getvalue()
        # version should be non-empty and match _get_version format
        self.assertTrue(output.strip())
        # also via main entry point
        stdout2 = io.StringIO()
        stderr2 = io.StringIO()
        with contextlib.redirect_stdout(stdout2), contextlib.redirect_stderr(stderr2), self.assertRaises(SystemExit) as cm2:
            main(["--version"])
        self.assertEqual(cm2.exception.code, 0)
        output2 = stdout2.getvalue() + stderr2.getvalue()
        self.assertTrue(output2.strip())

    def test_missing_path_exits_nonzero_with_message_and_no_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["index", "/nonexistent/path/xyz"])
        self.assertEqual(exit_code, 1)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertIn("does not exist", combined.lower())
        self.assertNotIn("Traceback", combined)
        # also for search with missing path
        stdout2 = io.StringIO()
        stderr2 = io.StringIO()
        with contextlib.redirect_stdout(stdout2), contextlib.redirect_stderr(stderr2):
            exit_code2 = main(["--db", self.db_path, "search", "hello", "/nope"])
        self.assertEqual(exit_code2, 1)
        combined2 = stdout2.getvalue() + stderr2.getvalue()
        self.assertIn("does not exist", combined2.lower())
        self.assertNotIn("Traceback", combined2)

    def test_status_oneline_emits_exactly_one_line(self):
        main(["--db", self.db_path, "index", str(self.root)])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["--db", self.db_path, "status", "--oneline", str(self.root)]
            )
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue().strip()
        lines = [l for l in output.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one line, got {lines!r}")
        lower = lines[0].lower()
        self.assertIn("symbols", lower)
        self.assertIn("chunks", lower)
        self.assertIn("pending", lower)


class TestCliInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_fresh_directory_creates_mcp_json(self):
        results = cmd_init(str(self.root))

        mcp_path = self.root / ".mcp.json"
        self.assertTrue(mcp_path.exists())
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertIn("mcpServers", data)
        self.assertIn("symbolgraph", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["symbolgraph"]["command"], "sg-mcp")
        self.assertEqual(results[str(mcp_path)], "written")

        # via CLI entry point as well
        exit_code = main(["init", str(self.root)])
        self.assertEqual(exit_code, 0)

    def test_init_preserves_existing_unrelated_server(self):
        mcp_path = self.root / ".mcp.json"
        mcp_path.write_text(
            json.dumps({"mcpServers": {"other": {"command": "other-server"}}}),
            encoding="utf-8",
        )

        results = cmd_init(str(self.root))

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertIn("other", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["other"]["command"], "other-server")
        self.assertIn("symbolgraph", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["symbolgraph"]["command"], "sg-mcp")
        self.assertEqual(results[str(mcp_path)], "written")

    def test_init_refuses_to_overwrite_malformed_json(self):
        mcp_path = self.root / ".mcp.json"
        original = '{"mcpServers": {"other": {"command": "other-server"},}'
        mcp_path.write_text(original, encoding="utf-8")

        with self.assertRaises(ValueError):
            cmd_init(str(self.root))

        self.assertEqual(mcp_path.read_text(encoding="utf-8"), original)

        exit_code = main(["init", str(self.root)])
        self.assertEqual(exit_code, 1)
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), original)

    def test_init_refuses_to_overwrite_non_object_json(self):
        mcp_path = self.root / ".mcp.json"
        mcp_path.write_text("[]", encoding="utf-8")

        with self.assertRaises(ValueError):
            cmd_init(str(self.root))

        self.assertEqual(mcp_path.read_text(encoding="utf-8"), "[]")

    def test_init_idempotent_second_run_reports_already_configured(self):
        mcp_path = self.root / ".mcp.json"

        first = cmd_init(str(self.root))
        self.assertEqual(first[str(mcp_path)], "written")
        content_first = mcp_path.read_text(encoding="utf-8")

        second = cmd_init(str(self.root))
        self.assertEqual(second[str(mcp_path)], "already configured")
        content_second = mcp_path.read_text(encoding="utf-8")
        self.assertEqual(content_first, content_second)

        # also via main, should print already configured and return 0
        exit_code = main(["init", str(self.root)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), content_second)

    def test_init_writes_vscode_and_cursor_when_dirs_present(self):
        (self.root / ".vscode").mkdir()
        (self.root / ".cursor").mkdir()

        results = cmd_init(str(self.root))

        vscode_path = self.root / ".vscode" / "mcp.json"
        cursor_path = self.root / ".cursor" / "mcp.json"
        self.assertTrue(vscode_path.exists())
        self.assertTrue(cursor_path.exists())
        # Cursor keys servers under "mcpServers"; VS Code under "servers".
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["mcpServers"]["symbolgraph"]["command"], "sg-mcp")
        vscode = json.loads(vscode_path.read_text(encoding="utf-8"))
        self.assertEqual(vscode["servers"]["symbolgraph"]["command"], "sg-mcp")
        self.assertEqual(results[str(vscode_path)], "written")
        self.assertEqual(results[str(cursor_path)], "written")

    def test_init_updates_opencode_json_when_present(self):
        opencode_path = self.root / "opencode.json"
        opencode_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

        results = cmd_init(str(self.root))

        data = json.loads(opencode_path.read_text(encoding="utf-8"))
        self.assertEqual(data["foo"], "bar")
        self.assertIn("mcp", data)
        self.assertIn("symbolgraph", data["mcp"])
        # opencode takes an argv list plus an explicit transport.
        self.assertEqual(data["mcp"]["symbolgraph"]["command"], ["sg-mcp"])
        self.assertEqual(data["mcp"]["symbolgraph"]["type"], "local")
        self.assertEqual(results[str(opencode_path)], "written")

    def test_init_does_not_create_vscode_when_dir_missing(self):
        results = cmd_init(str(self.root))

        vscode_path = self.root / ".vscode" / "mcp.json"
        self.assertFalse(vscode_path.exists())
        self.assertNotIn(str(vscode_path), results)


if __name__ == "__main__":
    unittest.main()
