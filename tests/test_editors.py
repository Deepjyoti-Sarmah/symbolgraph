import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from symbolgraph.cli import (
    _ensure_mcp_entry,
    _resolve_editor_path,
    _write_codex_entry,
    cmd_init,
    cmd_uninstall,
)
from symbolgraph.editors import (
    EDITORS,
    SG_BLOCK_CONTENT,
    SG_BLOCK_START,
    atomic_write_text,
    codex_config_path,
    codex_section_name,
    detect_editors,
    ensure_block_content,
    project_storage_slug,
    remove_block_content,
    toml_escape,
)


class TestEditors(unittest.TestCase):
    def setUp(self):
        # `--agent all` covers codex, whose config lives outside the repo. Point
        # it at a temp dir so no test can touch the real ~/.codex/config.toml.
        self._codex_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._codex_home.cleanup)
        patcher = unittest.mock.patch.dict(
            "os.environ", {"CODEX_HOME": self._codex_home.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_editors_has_8(self):
        self.assertGreaterEqual(len(EDITORS), 8)

    def test_every_editor_has_an_instructions_file(self):
        # Registering the MCP server without telling the agent about it is the
        # configuration that produced zero tool calls in practice.
        for name, info in EDITORS.items():
            with self.subTest(editor=name):
                self.assertTrue(info.get("instructions_path"), name)

    def test_claude_instructions_go_to_claude_md(self):
        # Claude Code reads CLAUDE.md; it does not read AGENTS.md.
        self.assertEqual(EDITORS["claude"]["instructions_path"], "CLAUDE.md")

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.json"
            atomic_write_text(p, '{"a":1}')
            self.assertEqual(p.read_text(), '{"a":1}')

    def test_atomic_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_write_text(root / "a.txt", "hello")
            self.assertEqual([p.name for p in root.iterdir()], ["a.txt"])

    def test_detect_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vscode").mkdir()
            self.assertIn("vscode", detect_editors(root))

    def test_detect_claude_from_marker_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("# rules")
            self.assertIn("claude", detect_editors(root))

    def test_ensure_mcp_entry_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".mcp.json"
            p.write_text(json.dumps({"mcpServers": {"symbolgraph": {"command": "sg-mcp"}}}))
            self.assertEqual(_ensure_mcp_entry(p, "mcpServers"), "already configured")

    def test_ensure_mcp_entry_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".mcp.json"
            p.write_text("not json")
            with self.assertRaises(ValueError):
                _ensure_mcp_entry(p, "mcpServers")

    def test_init_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vscode").mkdir()
            (root / ".cursor").mkdir()
            (root / "opencode.json").write_text("{}")
            results = cmd_init(str(root), agents=["all"])
            self.assertIn(str(root / ".mcp.json"), results)
            self.assertIn(str(root / ".vscode/mcp.json"), results)

    def test_init_all_writes_each_shared_file_once(self):
        # AGENTS.md is the instruction file for several editors; a single run
        # must not report it as "already configured" against itself.
        with tempfile.TemporaryDirectory() as tmp:
            results = cmd_init(tmp, agents=["all"])
            shared = str(Path(tmp) / "AGENTS.md")
            self.assertEqual(results[shared], "written")

    def test_init_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = cmd_init(str(root), agents=["auto"])
            self.assertIn(str(root / ".mcp.json"), results)

    def test_init_auto_writes_claude_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_init(str(root), agents=["auto"])
            self.assertIn(SG_BLOCK_START, (root / "CLAUDE.md").read_text())

    def test_init_preserves_existing_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("# House rules\n\nBe careful.\n")
            cmd_init(str(root), agents=["claude"])
            text = (root / "CLAUDE.md").read_text()
            self.assertIn("# House rules", text)
            self.assertIn(SG_BLOCK_START, text)

    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_init(str(root), agents=["claude"])
            results = cmd_init(str(root), agents=["claude"])
            self.assertEqual(results[str(root / "CLAUDE.md")], "already configured")
            self.assertEqual((root / "CLAUDE.md").read_text().count("end sg-block"), 1)

    def test_init_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _results = cmd_init(str(root), agents=["pi"])
            self.assertTrue((root / "AGENTS.md").exists())

    def test_vscode_uses_servers_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_init(str(root), agents=["vscode"])
            data = json.loads((root / ".vscode/mcp.json").read_text())
            self.assertIn("symbolgraph", data["servers"])
            self.assertEqual(data["servers"]["symbolgraph"]["type"], "stdio")

    def test_opencode_entry_uses_argv_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_init(str(root), agents=["opencode"])
            entry = json.loads((root / "opencode.json").read_text())["mcp"]["symbolgraph"]
            self.assertEqual(entry["command"], ["sg-mcp"])
            self.assertEqual(entry["type"], "local")

    def test_codex_registers_each_project_separately(self):
        # ~/.codex/config.toml is global: a project-agnostic marker made every
        # project after the first silently skip registration. Driving
        # _write_codex_entry directly keeps this off ~ expansion, which reads
        # HOME on POSIX but USERPROFILE on Windows.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            a, b = root / "a", root / "b"
            a.mkdir()
            b.mkdir()
            self.assertEqual(_write_codex_entry(config, a), "written")
            self.assertEqual(_write_codex_entry(config, b), "written")
            self.assertEqual(_write_codex_entry(config, a), "already configured")
            text = config.read_text()
            self.assertIn(f"[mcp_servers.{codex_section_name(a)}]", text)
            self.assertIn(f"[mcp_servers.{codex_section_name(b)}]", text)

    def test_codex_config_honors_codex_home(self):
        # The env override is what keeps this suite (and `--agent all`) away
        # from the real config on every platform.
        self.assertEqual(
            codex_config_path(), Path(self._codex_home.name) / "config.toml"
        )
        self.assertEqual(
            _resolve_editor_path(Path("/repo"), "~/.codex/config.toml"),
            codex_config_path(),
        )

    def test_init_all_never_touches_the_real_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_init(tmp, agents=["all"])
            written = Path(self._codex_home.name) / "config.toml"
            self.assertTrue(written.exists())
            self.assertIn(
                f"[mcp_servers.{codex_section_name(Path(tmp))}]", written.read_text()
            )

    def test_uninstall_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("# House rules\n\nBe careful.\n")
            cmd_init(str(root), agents=["claude", "pi"])
            cmd_uninstall(str(root))
            # Our block is gone, the user's own content survives, and a file
            # that only ever held our block is removed entirely.
            self.assertEqual((root / "CLAUDE.md").read_text(), "# House rules\n\nBe careful.\n")
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertNotIn(
                "symbolgraph", json.loads((root / ".mcp.json").read_text())["mcpServers"]
            )

    def test_toml_escape(self):
        self.assertEqual(toml_escape('a\\b"c'), 'a\\\\b\\"c')
        self.assertIn("-", project_storage_slug("/tmp/foo\\bar"))
        content, already = ensure_block_content("")
        self.assertFalse(already)
        self.assertIn("sg-block-version", content)
        _content2, already2 = ensure_block_content(content)
        self.assertTrue(already2)
        # legacy upgrade
        old = "<!-- symbolgraph MCP: sg-mcp -->\nhello"
        new, _ = ensure_block_content(old)
        self.assertIn("sg-block-version", new)

    def test_block_upgrade_replaces_instead_of_appending(self):
        stale = SG_BLOCK_CONTENT.replace("sg-block-version: ", "sg-block-version: 0")
        existing = f"# Rules\n\n{stale}\n\nTrailing prose.\n"
        new, already = ensure_block_content(existing)
        self.assertFalse(already)
        self.assertEqual(new.count("end sg-block"), 1)
        self.assertIn(SG_BLOCK_START, new)
        self.assertIn("# Rules", new)
        self.assertIn("Trailing prose.", new)

    def test_remove_block_content(self):
        text, _ = ensure_block_content("# Rules\n\nkeep me\n")
        stripped, changed = remove_block_content(text)
        self.assertTrue(changed)
        self.assertEqual(stripped, "# Rules\n\nkeep me\n")
        self.assertEqual(remove_block_content("nothing here\n"), ("nothing here\n", False))

    def test_block_names_the_mcp_tools(self):
        # The block is the whole point of `sg init`: if it stops naming the
        # tools, agents go back to grepping.
        for tool in ("definition", "callers", "callees", "imports", "search", "context"):
            self.assertIn(tool, SG_BLOCK_CONTENT, tool)
        self.assertNotIn("symbolgraph symbolgraph", SG_BLOCK_CONTENT)


if __name__ == "__main__":
    unittest.main()
