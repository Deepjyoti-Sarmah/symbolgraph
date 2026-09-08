"""Multi-editor config matrix — writes/detects MCP server entries per editor.

Two things have to land for an agent to actually use symbolgraph:

1. the MCP server has to be registered (``config_path``/``container_key``), and
2. the agent has to be *told* to prefer it over grep (``instructions_path``).

Registering the server alone is not enough — an agent with 15 unmentioned tools
keeps grepping. Every editor therefore also gets the instruction block written
to the file that that editor actually reads: Claude Code reads ``CLAUDE.md``,
not ``AGENTS.md``.
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

# `entry` is the MCP server entry shape that editor expects; editors disagree
# (VS Code wants an explicit transport, opencode wants an argv list).
_STDIO_ENTRY: dict[str, object] = {"command": "sg-mcp"}

EDITORS: dict[str, dict] = {
    "claude": {
        "config_path": ".mcp.json",
        "container_key": "mcpServers",
        "entry": _STDIO_ENTRY,
        "instructions_path": "CLAUDE.md",
    },
    "cursor": {
        "config_path": ".cursor/mcp.json",
        "container_key": "mcpServers",
        "entry": _STDIO_ENTRY,
        "instructions_path": "AGENTS.md",
    },
    "vscode": {
        # VS Code's mcp.json keys servers under "servers", not "mcpServers".
        "config_path": ".vscode/mcp.json",
        "container_key": "servers",
        "entry": {"type": "stdio", "command": "sg-mcp"},
        "instructions_path": ".github/copilot-instructions.md",
    },
    "opencode": {
        "config_path": "opencode.json",
        "container_key": "mcp",
        "entry": {"type": "local", "command": ["sg-mcp"], "enabled": True},
        "instructions_path": "AGENTS.md",
    },
    "gemini": {
        "config_path": ".gemini/settings.json",
        "container_key": "mcpServers",
        "entry": _STDIO_ENTRY,
        "instructions_path": "GEMINI.md",
    },
    "copilot": {
        "config_path": None,
        "container_key": None,
        "entry": None,
        "instructions_path": ".github/copilot-instructions.md",
    },
    "pi": {
        "config_path": None,
        "container_key": None,
        "entry": None,
        "instructions_path": "AGENTS.md",
    },
    "codex": {
        "config_path": "~/.codex/config.toml",
        "container_key": "mcp_servers",
        "entry": _STDIO_ENTRY,
        "instructions_path": "AGENTS.md",
    },
}


SG_BLOCK_VERSION = 2
SG_BLOCK_START = f"<!-- sg-block-version: {SG_BLOCK_VERSION} -->"
SG_BLOCK_END = "<!-- end sg-block -->"

# Matches an sg block of *any* version so an upgrade replaces in place instead
# of stacking a second copy underneath the old one.
_SG_BLOCK_RE = re.compile(
    r"<!-- sg-block-version: \d+ -->.*?<!-- end sg-block -->",
    re.DOTALL,
)
_LEGACY_MARKER = "<!-- symbolgraph MCP: sg-mcp -->"

SG_BLOCK_BODY = """## symbolgraph — query the code index before reading files

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
`sg callers X`, `sg callees X`."""

SG_BLOCK_CONTENT = f"{SG_BLOCK_START}\n{SG_BLOCK_BODY}\n{SG_BLOCK_END}"


def toml_escape(s: str) -> str:
    """Escape backslashes and double quotes for a TOML string value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def project_storage_slug(abs_path: str) -> str:
    h = hashlib.sha256(abs_path.encode()).hexdigest()[:6]
    # sanitize  basename for filesystem/ TOML safety
    base = Path(abs_path).name.replace("\\", "_").replace('"', "_")
    return f"{base}-{h}"


def codex_config_path() -> Path:
    """Codex's config file.

    Honors ``CODEX_HOME`` the way codex itself does, so we write where codex
    actually reads. It also gives tests a cross-platform way to redirect this:
    ``~`` expansion reads ``HOME`` on POSIX but ``USERPROFILE`` on Windows, so
    patching ``HOME`` silently wrote into the real home directory there.
    """
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "config.toml"
    return Path("~/.codex/config.toml").expanduser()


def codex_section_name(root: Path) -> str:
    """The codex TOML table for one project — unique per project path.

    Codex config is global (``~/.codex/config.toml``), so the marker used to
    decide "already configured" has to name *this* project. A bare `sg-mcp`
    marker made every project after the first a silent no-op.
    """
    return f"sg-{toml_escape(project_storage_slug(str(root.resolve())))}"


def ensure_block_content(existing: str) -> tuple[str, bool]:
    """Return (new_content, already_configured), upgrading any older sg block."""
    match = _SG_BLOCK_RE.search(existing)
    if match is not None:
        if match.group(0) == SG_BLOCK_CONTENT:
            return existing, True
        # Older (or hand-edited) block — replace it in place.
        return existing[: match.start()] + SG_BLOCK_CONTENT + existing[match.end() :], False
    if _LEGACY_MARKER in existing:
        return existing.replace(_LEGACY_MARKER, SG_BLOCK_CONTENT), False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing:
        existing += "\n"
    return existing + SG_BLOCK_CONTENT + "\n", False


def remove_block_content(existing: str) -> tuple[str, bool]:
    """Strip the sg block (any version) from an instruction file."""
    new = _SG_BLOCK_RE.sub("", existing)
    new = new.replace(_LEGACY_MARKER, "")
    if new == existing:
        return existing, False
    return new.strip("\n") + "\n" if new.strip() else "", True


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        os.write(fd, content.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def detect_editors(root: Path) -> list[str]:
    detected = []
    if (root / ".claude").is_dir() or (root / "CLAUDE.md").exists() or (root / ".mcp.json").exists():
        detected.append("claude")
    if (root / ".vscode").is_dir():
        detected.append("vscode")
    if (root / ".cursor").is_dir():
        detected.append("cursor")
    if (root / "opencode.json").exists():
        detected.append("opencode")
    if (root / ".gemini").is_dir():
        detected.append("gemini")
    if (root / ".github" / "copilot-instructions.md").exists():
        detected.append("copilot")
    if not detected:
        detected.append("claude")
    return detected
