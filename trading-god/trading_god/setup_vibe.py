"""Point Vibe-Trading at the desk, OpenBB, moomoo and the playbooks.

``install.ps1`` runs this once after installing the packages. It only adds or
updates the keys Trading God owns; every other setting in ``~/.vibe-trading``
is kept, and each file it changes is backed up first.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_god.config import SKILLS_DIR, TEMPLATE_DIR, home_dir

INVESTORSKILLS_ZIP = "https://github.com/langyuan-7/investorskills/archive/refs/heads/main.zip"
DEFAULT_MODEL = "sonnet"  # Claude Code alias for the latest Sonnet; "opus" is deeper but uses more of your plan
DEFAULT_OPENBB_URL = "http://127.0.0.1:8001/mcp"

#: Marks a skill folder this tool manages, so a later run may replace or remove it.
MANAGED_MARKER = ".trading-god-managed"


def vibe_home() -> Path:
    override = os.environ.get("VIBE_TRADING_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".vibe-trading"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.bak-{_stamp()}")
    shutil.copy2(path, backup)
    return backup


# -- ~/.vibe-trading/.env ---------------------------------------------------


def merge_env(path: Path, updates: dict[str, str]) -> dict[str, Any]:
    """Set ``updates`` in a dotenv file, keeping every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added by Trading God setup")
        out.extend(f"{key}={value}" for key, value in remaining.items())
    text = "\n".join(out) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return {"path": str(path), "changed": False}
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "changed": True, "backup": str(backup) if backup else None}


def runnable_claude(path: str) -> str:
    """Swap an npm ``claude.ps1`` or ``claude.cmd`` shim for a real ``claude.exe``.

    Vibe-Trading starts Claude Code with ``subprocess``. It cannot run a
    PowerShell script at all ("[WinError 193] %1 is not a valid Win32
    application"), and through a batch file cmd.exe would mangle the quotes in
    the JSON schema argument.
    """
    shim = Path(path)
    if shim.suffix.lower() not in (".ps1", ".cmd", ".bat"):
        return path
    for exe in (shim.with_suffix(".exe"), Path.home() / ".local" / "bin" / "claude.exe"):
        if exe.is_file():
            return str(exe)
    raise ValueError(
        f"{path} is a script, which Vibe-Trading cannot start. Install the native Claude Code "
        "with: irm https://claude.ai/install.ps1 | iex"
    )


def env_updates(model: str, claude_path: str | None) -> dict[str, str]:
    updates = {
        # Drive the agent with the local Claude Code login: no API key, no per-token bill.
        "LANGCHAIN_PROVIDER": "claude-cli",
        "LANGCHAIN_MODEL_NAME": model,
        # Keep Vibe-Trading's own broker tools away from the model: the agent
        # reaches moomoo only through the desk, where a person approves orders.
        "CLAUDE_CLI_DENY_TOOL_PREFIXES": "trading_,etoro_",
        "CLAUDE_CLI_BASH_POLICY": "python-only",
        # Runs the daily pre-market scan. Vibe-Trading's own system jobs keep
        # their separate switches, so this starts nothing else.
        "VIBE_TRADING_ENABLE_SCHEDULER": "1",
        # A full scan takes more than Vibe-Trading's default 50 steps; at 80% of
        # the budget the agent is told to wrap up, and it skipped the research.
        "VIBE_TRADING_AGENT_MAX_ITERATIONS": "120",
    }
    if claude_path:
        updates["CLAUDE_CLI_PATH"] = runnable_claude(claude_path)
    return updates


# -- ~/.vibe-trading/agent.json -----------------------------------------------


#: The desk tools the agent may use. Listing them means a tool added to the
#: server later stays hidden until someone adds it here on purpose.
DESK_TOOLS = ["get_portfolio", "propose_order", "list_proposals", "withdraw_proposal"]

#: OpenBB's discovery tools: search for a data tool, then run it through call_tool.
#: This keeps hundreds of tool schemas out of every prompt.
OPENBB_TOOLS = ["available_categories", "available_tools", "search_tools", "call_tool"]


def mcp_servers(desk_python: str, openbb_url: str) -> dict[str, dict[str, Any]]:
    return {
        "desk": {
            "command": desk_python,
            "args": ["-m", "trading_god.mcp_server"],
            "toolTimeout": 60,
            "initTimeout": 60,
            "enabledTools": DESK_TOOLS,
        },
        "openbb": {
            "type": "streamableHttp",
            "url": openbb_url,
            "toolTimeout": 120,
            "initTimeout": 30,
            "enabledTools": OPENBB_TOOLS,
        },
    }


def merge_agent_config(path: Path, servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add or replace our MCP servers in ``agent.json``, keeping everything else."""
    for other in ("agent.yaml", "agent.yml"):
        if (path.parent / other).exists():
            raise RuntimeError(
                f"{path.parent / other} exists and Vibe-Trading may read it instead of agent.json. "
                "Merge the mcpServers below into it by hand:\n" + json.dumps({"mcpServers": servers}, indent=2)
            )
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        data = json.loads(text) if text else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} does not hold a JSON object")
    key = "mcpServers" if "mcpServers" in data or "mcp_servers" not in data else "mcp_servers"
    existing = data.get(key) or {}
    merged = {**existing, **servers}
    if existing == merged:
        return {"path": str(path), "changed": False}
    data[key] = merged
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "changed": True, "backup": str(backup) if backup else None}


def validate_agent_config(path: Path) -> str | None:
    """Parse the file with Vibe-Trading's own loader; return the error, if any."""
    try:
        from src.config.loader import load_agent_config
    except Exception:  # noqa: BLE001 - Vibe-Trading not importable here; skip the check
        return None
    try:
        config = load_agent_config(path)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    missing = {"desk", "openbb"} - set(config.mcp_servers)
    return f"agent.json is missing {sorted(missing)}" if missing else None


# -- ~/.vibe-trading/futu.json (Vibe-Trading's own moomoo connector) -----------


def write_futu_config(host: str, port: int, security_firm: str, market: str) -> str:
    """Point Vibe-Trading's read-only moomoo views at the same paper account."""
    payload = {
        "host": host,
        "port": port,
        "profile": "paper",
        "security_firm": security_firm,
        "filter_trdmarket": market,
        "acc_id": 0,
        "readonly": True,
    }
    try:
        from src.trading.connectors.futu.sdk import FutuConfig, save_config

        return str(save_config(FutuConfig.from_mapping(payload)))
    except ImportError:
        path = vibe_home() / "futu.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return str(path)


# -- skills -------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index], "\n".join(lines[index + 1 :])
    return [], text


def playbook_skill(skill_md: str, invest_md: str | None, source: str) -> str:
    """One self-contained SKILL.md: Vibe-Trading's loader reads only SKILL.md, so ``invest.md`` is inlined."""
    meta, body = _split_frontmatter(skill_md)
    meta = [line for line in meta if not line.startswith("invest:")]
    if not any(line.startswith("category:") for line in meta):
        meta.append("category: playbook")
    meta.append(f"source: {source}")
    parts = ["---", *meta, "---", body.strip(), ""]
    if invest_md:
        invest_meta, invest_body = _split_frontmatter(invest_md)
        parts += ["## Investment system", "", "The structured rules for this playbook (from invest.md):", ""]
        if invest_meta:
            parts += ["```yaml", *invest_meta, "```", ""]
        if invest_body.strip():
            parts += [invest_body.strip(), ""]
    return "\n".join(parts)


def download_investorskills(target: Path) -> Path:
    """Download the investorskills fork into ``target``; return the folder holding ``skills/``."""
    with urllib.request.urlopen(INVESTORSKILLS_ZIP, timeout=60) as response:  # noqa: S310 - fixed GitHub URL
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    root = target.resolve()
    for member in archive.namelist():
        destination = (target / member).resolve()
        if root not in destination.parents and destination != root:
            raise RuntimeError(f"unsafe path in investorskills archive: {member}")
    archive.extractall(target)
    folders = [p for p in target.iterdir() if (p / "skills").is_dir()]
    if not folders:
        raise RuntimeError("the investorskills download has no skills folder")
    return folders[0]


def _replace_managed(dest: Path) -> None:
    if dest.exists():
        if not (dest / MANAGED_MARKER).exists():
            raise RuntimeError(f"{dest} exists and was not created by Trading God; rename it or remove it first")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / MANAGED_MARKER).write_text("Created by Trading God setup; rerunning setup replaces this folder.\n")


def install_skills(user_skills: Path, playbooks: list[str], investorskills: Path) -> list[str]:
    """Copy our own skills and the chosen playbooks; drop managed playbooks no longer chosen."""
    user_skills.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()):
        dest = user_skills / skill_dir.name
        _replace_managed(dest)
        shutil.copy2(skill_dir / "SKILL.md", dest / "SKILL.md")
        installed.append(skill_dir.name)
    for name in playbooks:
        source = investorskills / "skills" / name
        if not (source / "SKILL.md").is_file():
            raise RuntimeError(f"playbook {name!r} is not in investorskills (see config/playbooks.txt)")
        invest = source / "invest.md"
        text = playbook_skill(
            (source / "SKILL.md").read_text(encoding="utf-8"),
            invest.read_text(encoding="utf-8") if invest.is_file() else None,
            "github.com/langyuan-7/investorskills",
        )
        dest = user_skills / name
        _replace_managed(dest)
        (dest / "SKILL.md").write_text(text, encoding="utf-8")
        installed.append(name)
    for folder in user_skills.iterdir():
        if folder.is_dir() and (folder / MANAGED_MARKER).exists() and folder.name not in installed:
            shutil.rmtree(folder)
    return installed


def read_playbook_list(path: Path | None = None) -> list[str]:
    path = path or TEMPLATE_DIR / "playbooks.txt"
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


# -- user config ----------------------------------------------------------------


def seed_user_files() -> list[str]:
    """Copy desk.toml and watchlist.txt into ~/.trading-god once; never overwrite edits."""
    created = []
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    for name in ("desk.toml", "watchlist.txt"):
        target = home / name
        if not target.exists():
            shutil.copy2(TEMPLATE_DIR / name, target)
            created.append(str(target))
    return created


def configure(
    *,
    desk_python: str,
    openbb_url: str = DEFAULT_OPENBB_URL,
    model: str = DEFAULT_MODEL,
    claude_path: str | None = None,
    investorskills: Path | None = None,
) -> dict[str, Any]:
    from trading_god.config import load_settings

    seeded = seed_user_files()
    settings = load_settings()
    root = vibe_home()
    agent_json = root / "agent.json"
    summary: dict[str, Any] = {
        "user_files_created": seeded,
        "env": merge_env(root / ".env", env_updates(model, claude_path)),
        "agent_json": merge_agent_config(agent_json, mcp_servers(desk_python, openbb_url)),
        "futu_json": write_futu_config(
            settings.moomoo.host, settings.moomoo.port, settings.moomoo.security_firm, settings.moomoo.market
        ),
    }
    problem = validate_agent_config(agent_json)
    if problem:
        raise RuntimeError(f"Vibe-Trading could not read {agent_json}: {problem}")
    user_skills = root / "skills" / "user"
    if investorskills is not None:
        if not (investorskills / "skills").is_dir():
            raise RuntimeError(f"{investorskills} has no skills folder")
        summary["skills"] = install_skills(user_skills, read_playbook_list(), investorskills)
    else:
        with tempfile.TemporaryDirectory(prefix="investorskills-") as workdir:
            source = download_investorskills(Path(workdir))
            summary["skills"] = install_skills(user_skills, read_playbook_list(), source)
    return summary
