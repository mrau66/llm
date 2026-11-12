import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kb
from service import KnowledgeService


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The preparation sandbox blocks cross-thread event-loop wakeup sockets.
        # Test the service logic with inline offloads; Docker smoke tests cover
        # the real event loop, SDK, and network when deployed on the host.
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        offload = patch("service.asyncio.to_thread", side_effect=inline)
        offload.start()
        self.addCleanup(offload.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("DATA", self.root / "data"), ("VAULT", self.root / "vault")):
            p = patch.object(kb, name, value)
            p.start()
            self.addCleanup(p.stop)
        kb.VAULT.mkdir()
        kb.init()
        self.service = KnowledgeService()

    async def test_save_and_progress_work_without_discord_credentials(self):
        result = await self.service.save_link("https://example.com/article")
        self.assertEqual(result["status"], "queued")
        progress = await self.service.link_status("https://example.com/article")
        self.assertEqual(progress["status"], "queued")
        kb.finish_job("https://example.com/article")
        progress = await self.service.link_status("https://example.com/article")
        self.assertEqual(progress["status"], "done")
        self.assertTrue(progress["note"].startswith("Knowledge Inbox/"))

    async def test_search_returns_passages_for_zeroclaw_to_answer(self):
        row = {"title": "Soil", "url": "https://example.com/soil", "path": "soil.md", "text": "Mulch helps soil."}
        with patch.object(kb, "retrieve", return_value=[row]) as retrieve:
            result = await self.service.search("How does mulch help?", 999)
        retrieve.assert_called_once_with("How does mulch help?", 8)
        self.assertEqual(result["passages"][0]["citation"], 1)
        self.assertEqual(result["passages"][0]["url"], row["url"])
        self.assertEqual(result["passages"][0]["text"], row["text"])

    async def test_worker_completes_a_queued_link(self):
        await self.service.save_link("https://example.com/article")
        with patch.object(kb, "ingest", return_value=("Title", "note.md")):
            task = asyncio.create_task(self.service.worker())
            try:
                for _ in range(50):
                    if (await self.service.link_status("https://example.com/article"))["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual((await self.service.link_status("https://example.com/article"))["status"], "done")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_search_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.service.search(" ")
