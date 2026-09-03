"""Prepare an isolated Gateway workspace without changing or starting live services."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import tomllib
from pathlib import Path

import tomli_w

CANDIDATE_PARENT = Path("/mnt/data/opensquilla-dev/tmp")


def prepare(*, live: Path, source: Path, target: Path, port: int) -> dict[str, str | int]:
    live, source, target = live.resolve(), source.resolve(), target.resolve()
    allowed = CANDIDATE_PARENT.resolve()
    target.relative_to(allowed)
    if target == allowed or target.exists():
        raise ValueError("candidate target must be a new isolated temporary directory")
    if not 1024 <= port <= 65535:
        raise ValueError("candidate port is invalid")
    config = tomllib.loads((live / "config/gateway.toml").read_text())
    if port == config["port"]:
        raise ValueError("candidate must not use the live Gateway port")
    servers = config["mcp"]["servers"]
    if len(servers) != 1 or servers[0].get("name") != "knowledge-vnext":
        raise ValueError("candidate requires exactly the named knowledge-vnext MCP server")
    server = servers[0]
    code = source / "scripts/knowledge_research"
    for name in (
        "bridge.py",
        "media_bridge.py",
        "skill/SKILL.md",
        "workspace/AGENTS.md",
        "workspace/TOOLS.md",
    ):
        if not (code / name).is_file():
            raise ValueError(f"candidate source is missing {name}")
    original_env = {}
    for line in (live / "private/gateway.env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError(f"unsupported environment value for {key}")
            original_env[key] = parsed[0]
    if not original_env.get("OPENSQUILLA_KNOWLEDGE_API_KEY"):
        raise ValueError("Knowledge credential is unavailable")

    target.mkdir(mode=0o700)
    for name in (
        "workspace",
        "state",
        "home",
        "private",
        "config",
        "xdg-state",
        "xdg-config",
        "xdg-cache",
    ):
        (target / name).mkdir(mode=0o700)
    (target / "workspace/skills/knowledge-local-research").mkdir(parents=True, mode=0o700)
    shutil.copyfile(
        code / "skill/SKILL.md", target / "workspace/skills/knowledge-local-research/SKILL.md"
    )
    for name in ("AGENTS.md", "TOOLS.md"):
        shutil.copyfile(code / "workspace" / name, target / "workspace" / name)

    config["host"] = "127.0.0.1"
    config["port"] = port
    config["workspace_dir"] = str(target / "workspace")
    config["state_dir"] = str(target / "state")
    config.setdefault("skills", {}).update(
        workspace_dir=str(target / "workspace/skills"),
        managed_dir=str(target / "home/skills"),
        extra_dirs=[],
    )
    for tool in ("mcp_researchNavigate", "mcp_researchReadEvidence"):
        if tool not in config["tools"]["allow"]:
            config["tools"]["allow"].append(tool)
    server["args"] = [
        "-I",
        "-B",
        str(code / "bridge.py"),
        "--workspace",
        str(target / "workspace"),
        "--private-root",
        str(target / "private/research"),
        "--media-root",
        str(target / "workspace/knowledge-artifacts"),
        "--",
        server["command"],
        "-I",
        "-B",
        str(code / "media_bridge.py"),
    ]
    server["env"]["OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR"] = str(
        target / "workspace/knowledge-artifacts"
    )
    (target / "config/gateway.toml").write_text(tomli_w.dumps(config))

    environment = {
        "OPENSQUILLA_AUTH_MODE": "token",
        "OPENSQUILLA_AUTH_TOKEN": secrets.token_hex(32),
        "OPENSQUILLA_KNOWLEDGE_API_KEY": original_env["OPENSQUILLA_KNOWLEDGE_API_KEY"],
        "LANG": "C.UTF-8",
        "HOME": str(target / "home"),
        "OPENSQUILLA_HOME": str(target / "home"),
        "OPENSQUILLA_STATE_DIR": str(target / "state"),
        "XDG_STATE_HOME": str(target / "xdg-state"),
        "XDG_CONFIG_HOME": str(target / "xdg-config"),
        "XDG_CACHE_HOME": str(target / "xdg-cache"),
    }
    env_path = target / "private/gateway.env"
    with env_path.open("x") as stream:
        os.chmod(env_path, 0o600)
        stream.write("".join(f"{key}={json.dumps(value)}\n" for key, value in environment.items()))
    return {"target": str(target), "config": str(target / "config/gateway.toml"), "port": port}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    print(
        json.dumps(prepare(live=args.live, source=args.source, target=args.target, port=args.port))
    )


if __name__ == "__main__":
    main()
