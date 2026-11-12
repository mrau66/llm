import copy
import unittest

from integration import merge_instructions, plan_config


class IntegrationTests(unittest.TestCase):
    def test_legacy_update_preserves_discord_provider_and_existing_tools(self):
        config = {"default_provider": "existing", "api_key": "keep-this",
                  "workspace_dir": "/zeroclaw-data/workspace",
                  "channels_config": {"discord": {"bot_token": "keep-token", "guild_id": "123"}},
                  "mcp": {"enabled": False, "servers": [{"name": "other", "command": "other"}]}}
        original = copy.deepcopy(config)
        result, workspace, alias = plan_config(config, "/zeroclaw-data/.zeroclaw/config.toml")
        self.assertEqual(config, original)
        self.assertEqual(result["api_key"], "keep-this")
        self.assertEqual(result["channels_config"], original["channels_config"])
        self.assertEqual(result["mcp"]["servers"][0], original["mcp"]["servers"][0])
        self.assertEqual(workspace, "/zeroclaw-data/workspace")
        self.assertEqual(alias, "")
        again, _, _ = plan_config(result, "/zeroclaw-data/.zeroclaw/config.toml")
        self.assertEqual(again, result)

    def test_modern_update_grants_only_the_existing_discord_agent(self):
        config = {"channels": {"discord": {}}, "agents": {
            "main": {"channels": ["discord.main"], "model_provider": "existing",
                     "mcp_bundles": ["existing"], "workspace": {"path": "/data/main"}},
            "other": {"channels": ["telegram.other"], "model_provider": "other"}},
            "mcp_bundles": {"existing": {"servers": ["files"]}}}
        result, workspace, alias = plan_config(config, "/data/config.toml")
        self.assertEqual(alias, "main")
        self.assertEqual(workspace, "/data/main")
        self.assertEqual(result["agents"]["main"]["mcp_bundles"], ["existing", "personal_knowledge"])
        self.assertEqual(result["agents"]["other"], config["agents"]["other"])
        self.assertEqual(result["mcp_bundles"]["existing"], config["mcp_bundles"]["existing"])
        again, _, _ = plan_config(result, "/data/config.toml")
        self.assertEqual(again, result)

    def test_ambiguous_agent_requires_selection(self):
        config = {"channels": {}, "agents": {
            "one": {"channels": ["discord.one"]}, "two": {"channels": ["discord.two"]}}}
        with self.assertRaisesRegex(ValueError, "ZEROCLAW_AGENT"):
            plan_config(config, "/data/config.toml")
        _, _, alias = plan_config(config, "/data/config.toml", agent_alias="two")
        self.assertEqual(alias, "two")

    def test_existing_knowledge_server_conflict_is_not_overwritten(self):
        config = {"mcp": {"servers": [{"name": "knowledge", "url": "https://elsewhere"}]}}
        with self.assertRaisesRegex(ValueError, "already exists"):
            plan_config(config, "/data/config.toml")

    def test_existing_explicit_deny_is_not_bypassed(self):
        config = {"channels": {}, "agents": {"main": {"mcp_bundles": ["restricted"]}},
                  "mcp_bundles": {"restricted": {"exclude": ["knowledge"]}}}
        with self.assertRaisesRegex(ValueError, "denies"):
            plan_config(config, "/data/config.toml")

    def test_append_instructions_is_idempotent_and_preserves_existing_text(self):
        content = "<!-- BEGIN PERSONAL KNOWLEDGE -->\nNew instructions\n<!-- END PERSONAL KNOWLEDGE -->"
        original = "# Existing instructions\nKeep these.\n"
        result = merge_instructions(original, content)
        self.assertTrue(result.startswith(original.strip()))
        self.assertEqual(merge_instructions(result, content), result)
        updated = merge_instructions(result, content.replace("New", "Updated"))
        self.assertIn("Updated instructions", updated)
        self.assertEqual(updated.count("BEGIN PERSONAL KNOWLEDGE"), 1)

    def test_incomplete_instruction_markers_fail_safely(self):
        with self.assertRaises(ValueError):
            merge_instructions("<!-- BEGIN PERSONAL KNOWLEDGE -->", "new")
