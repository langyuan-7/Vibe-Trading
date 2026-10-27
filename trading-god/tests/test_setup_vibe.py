import json
from pathlib import Path

import pytest

from trading_god import setup_vibe
from trading_god.config import home_dir
from trading_god.setup_vibe import (
    MANAGED_MARKER,
    configure,
    env_updates,
    runnable_claude,
    install_skills,
    mcp_servers,
    merge_agent_config,
    merge_env,
    playbook_skill,
    read_playbook_list,
    seed_user_files,
    validate_agent_config,
    vibe_home,
)

SKILL_MD = """---
name: demo-playbook
description: "Use when testing."
invest: ./invest.md
---

# Demo playbook

Buy good things.
"""

INVEST_MD = """---
version: alpha
name: Demo
timeHorizon: days-weeks
---

# Rules

Sell bad things.
"""


@pytest.fixture
def investorskills(tmp_path):
    root = tmp_path / "investorskills"
    for name in read_playbook_list() + ["demo-playbook"]:
        folder = root / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(SKILL_MD.replace("demo-playbook", name), encoding="utf-8")
        (folder / "invest.md").write_text(INVEST_MD, encoding="utf-8")
    return root


def test_env_merge_keeps_other_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# mine\nOPENAI_API_KEY=keep-me\nLANGCHAIN_PROVIDER=openai\n", encoding="utf-8")
    result = merge_env(path, env_updates("sonnet", "C:/Users/lang/.local/bin/claude.exe"))
    text = path.read_text(encoding="utf-8")
    assert result["changed"] and result["backup"]
    assert "OPENAI_API_KEY=keep-me" in text and "# mine" in text
    assert "LANGCHAIN_PROVIDER=claude-cli" in text and "LANGCHAIN_PROVIDER=openai" not in text
    assert "CLAUDE_CLI_DENY_TOOL_PREFIXES=trading_,etoro_" in text
    assert "VIBE_TRADING_AGENT_MAX_ITERATIONS=120" in text
    assert "CLAUDE_CLI_PATH=C:/Users/lang/.local/bin/claude.exe" in text
    assert merge_env(path, env_updates("sonnet", "C:/Users/lang/.local/bin/claude.exe")) == {
        "path": str(path),
        "changed": False,
    }


def test_agent_config_merge_keeps_other_servers(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}, "other": 1}), encoding="utf-8")
    merge_agent_config(path, mcp_servers("C:/py/python.exe", "http://127.0.0.1:8001/mcp"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"mine", "desk", "openbb"}
    assert data["other"] == 1
    assert data["mcpServers"]["desk"]["args"] == ["-m", "trading_god.mcp_server"]
    assert data["mcpServers"]["openbb"]["enabledTools"] == setup_vibe.OPENBB_TOOLS


def test_agent_yaml_is_not_silently_shadowed(tmp_path):
    (tmp_path / "agent.yaml").write_text("mcpServers: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="agent.yaml"):
        merge_agent_config(tmp_path / "agent.json", mcp_servers("python", "http://127.0.0.1:8001/mcp"))


def test_vibe_trading_accepts_the_generated_agent_json(tmp_path):
    pytest.importorskip("src.config.loader")
    path = tmp_path / "agent.json"
    merge_agent_config(path, mcp_servers("python", "http://127.0.0.1:8001/mcp"))
    assert validate_agent_config(path) is None


def test_playbook_skill_inlines_the_investment_system():
    text = playbook_skill(SKILL_MD, INVEST_MD, "github.com/langyuan-7/investorskills")
    assert "invest:" not in text.split("---")[1]
    assert "category: playbook" in text
    assert "## Investment system" in text
    assert "```yaml\nversion: alpha" in text
    assert "Sell bad things." in text


def test_vibe_trading_reads_the_playbook_frontmatter():
    frontmatter = pytest.importorskip("src.agent.frontmatter")
    meta, body = frontmatter.parse_frontmatter(playbook_skill(SKILL_MD, INVEST_MD, "here"))
    assert meta["name"] == "demo-playbook"
    assert meta["category"] == "playbook"
    assert body.startswith("# Demo playbook")


def test_install_skills_replaces_only_what_it_manages(tmp_path, investorskills):
    user_skills = tmp_path / "skills" / "user"
    stale = user_skills / "old-playbook"
    stale.mkdir(parents=True)
    (stale / MANAGED_MARKER).write_text("", encoding="utf-8")
    mine = user_skills / "my-own-skill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("---\nname: my-own-skill\n---\n", encoding="utf-8")

    installed = install_skills(user_skills, ["demo-playbook"], investorskills)

    assert installed == ["trading-god-desk", "demo-playbook"]
    assert not stale.exists()
    assert (mine / "SKILL.md").exists()
    assert "## Investment system" in (user_skills / "demo-playbook" / "SKILL.md").read_text(encoding="utf-8")
    assert install_skills(user_skills, ["demo-playbook"], investorskills) == installed


def test_install_skills_will_not_overwrite_a_folder_it_did_not_create(tmp_path, investorskills):
    user_skills = tmp_path / "skills" / "user"
    (user_skills / "demo-playbook").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="not created by Trading God"):
        install_skills(user_skills, ["demo-playbook"], investorskills)


def test_unknown_playbook_is_an_error(tmp_path, investorskills):
    with pytest.raises(RuntimeError, match="nope"):
        install_skills(tmp_path / "skills", ["nope"], investorskills)


def test_user_files_are_seeded_once():
    created = seed_user_files()
    assert {Path(p).name for p in created} == {"desk.toml", "watchlist.txt"}
    (home_dir() / "watchlist.txt").write_text("TSLA\n", encoding="utf-8")
    assert seed_user_files() == []
    assert (home_dir() / "watchlist.txt").read_text(encoding="utf-8") == "TSLA\n"


def test_configure_wires_everything(investorskills):
    summary = configure(desk_python="C:/py/python.exe", claude_path="claude.exe", investorskills=investorskills)
    root = vibe_home()
    env = (root / ".env").read_text(encoding="utf-8")
    assert "LANGCHAIN_PROVIDER=claude-cli" in env and "VIBE_TRADING_ENABLE_SCHEDULER=1" in env
    agent = json.loads((root / "agent.json").read_text(encoding="utf-8"))
    assert agent["mcpServers"]["desk"]["command"] == "C:/py/python.exe"
    futu = json.loads((root / "futu.json").read_text(encoding="utf-8"))
    assert (futu["profile"], futu["security_firm"], futu["filter_trdmarket"]) == ("paper", "FUTUINC", "US")
    assert summary["skills"] == ["trading-god-desk", *read_playbook_list()]
    assert (root / "skills" / "user" / "trading-god-desk" / "SKILL.md").exists()


def test_the_desk_skill_names_only_real_tools():
    text = (setup_vibe.SKILLS_DIR / "trading-god-desk" / "SKILL.md").read_text(encoding="utf-8")
    for tool in setup_vibe.DESK_TOOLS:
        assert f"mcp_desk_{tool}" in text
    for tool in ("search_tools", "call_tool"):
        assert f"mcp_openbb_{tool}" in text
    for playbook in read_playbook_list():
        assert f"`{playbook}`" in text


def test_claude_script_shims_are_swapped_for_the_native_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    npm = tmp_path / "npm"
    npm.mkdir()
    for name in ("claude.ps1", "claude.cmd"):
        (npm / name).write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="install.ps1"):
        runnable_claude(str(npm / "claude.ps1"))
    native = tmp_path / "home" / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    assert runnable_claude(str(npm / "claude.ps1")) == str(native)
    assert env_updates("sonnet", str(npm / "claude.cmd"))["CLAUDE_CLI_PATH"] == str(native)
    (npm / "claude.exe").write_text("", encoding="utf-8")
    assert runnable_claude(str(npm / "claude.ps1")) == str(npm / "claude.exe")
    assert runnable_claude("/usr/local/bin/claude") == "/usr/local/bin/claude"
