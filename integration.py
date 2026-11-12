"""Plan an additive ZeroClaw configuration update without touching credentials."""
import copy
import json
import re
from pathlib import PurePosixPath


def plan_config(config, config_path, agent_alias="", workspace_override=""):
    result = copy.deepcopy(config)
    mcp = result.setdefault("mcp", {})
    mcp["enabled"] = True
    servers = mcp.setdefault("servers", [])
    existing = next((server for server in servers if server.get("name") == "knowledge"), None)
    endpoint = "http://knowledge:8000/mcp"
    if existing is not None and existing.get("url") != endpoint:
        raise ValueError("An MCP server named knowledge already exists with a different URL.")
    if existing is None:
        servers.append({"name": "knowledge", "transport": "http", "url": endpoint,
                        "headers": {"Accept": "application/json, text/event-stream"}})

    agents = result.get("agents", {})
    modern = "channels_config" not in result and (
        "channels" in result or any("model_provider" in value or "mcp_bundles" in value
                                    for value in agents.values()))
    root = PurePosixPath(config_path).parent
    alias = ""
    if modern:
        if agent_alias:
            candidates = [agent_alias] if agent_alias in agents else []
        else:
            candidates = [name for name, value in agents.items()
                          if value.get("enabled", True) and any(
                              str(channel).startswith("discord") for channel in value.get("channels", []))]
            if not candidates and len(agents) == 1:
                candidates = list(agents)
        if len(candidates) != 1:
            raise ValueError("Set ZEROCLAW_AGENT to the existing Discord agent alias. Available aliases: "
                             + ", ".join(agents))
        alias = candidates[0]
        agent = agents[alias]
        bundle = result.setdefault("mcp_bundles", {}).setdefault("personal_knowledge", {"servers": []})
        if "knowledge" in bundle.get("exclude", []):
            raise ValueError("personal_knowledge bundle explicitly excludes knowledge; resolve this first.")
        if "knowledge" not in bundle.setdefault("servers", []):
            bundle["servers"].append("knowledge")
        if "personal_knowledge" not in agent.setdefault("mcp_bundles", []):
            agent["mcp_bundles"].append("personal_knowledge")
        for bundle_name in agent["mcp_bundles"]:
            if "knowledge" in result["mcp_bundles"].get(bundle_name, {}).get("exclude", []):
                raise ValueError("An existing agent bundle denies knowledge. Resolve its exclusion first.")
        workspace = agent.get("workspace", {}).get("path") or str(root / "agents" / alias / "workspace")
        identity = agent.get("identity", {}).get("format", "openclaw")
    else:
        workspace = result.get("workspace_dir") or str(root / "workspace")
        identity = result.get("identity", {}).get("format", "openclaw")
    if identity != "openclaw":
        raise ValueError("This installer needs an OpenClaw-style AGENTS.md workspace; identity format differs.")
    workspace = workspace_override or workspace
    if not PurePosixPath(workspace).is_absolute():
        raise ValueError("Set ZEROCLAW_WORKSPACE to the absolute path of the existing agent workspace.")
    return result, workspace, alias


def merge_instructions(existing, instructions):
    begin = "<!-- BEGIN PERSONAL KNOWLEDGE -->"
    end = "<!-- END PERSONAL KNOWLEDGE -->"
    if (begin in existing) != (end in existing):
        raise ValueError("Existing knowledge instruction markers are incomplete; inspect AGENTS.md.")
    if begin in existing:
        return re.sub(re.escape(begin) + r".*?" + re.escape(end),
                      lambda _: instructions.strip(), existing, flags=re.DOTALL)
    return existing.rstrip() + "\n\n" + instructions.strip() + "\n"


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    import tomlkit

    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="/integration")
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--agent", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--instructions", action="store_true")
    args = parser.parse_args()
    directory = Path(args.directory)
    if args.instructions:
        original = (directory / "AGENTS.original.md").read_text()
        content = (directory / "instructions.md").read_text()
        (directory / "AGENTS.updated.md").write_text(merge_instructions(original, content))
    else:
        doc = tomlkit.parse((directory / "config.original.toml").read_text())
        merged, workspace, alias = plan_config(doc.unwrap(), args.config_path, args.agent, args.workspace)
        # Preserve all unrelated sections/comments and every existing provider/Discord value.
        doc["mcp"] = tomlkit.item(merged["mcp"])
        if alias:
            doc["mcp_bundles"] = tomlkit.item(merged["mcp_bundles"])
            doc["agents"][alias]["mcp_bundles"] = tomlkit.item(merged["agents"][alias]["mcp_bundles"])
        (directory / "config.updated.toml").write_text(tomlkit.dumps(doc))
        (directory / "plan.json").write_text(json.dumps({"workspace": workspace, "agent": alias}))
