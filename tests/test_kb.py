import hashlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import kb


def vectors(texts, query=False):
    # Deliberately distinguish two topics to exercise semantic retrieval.
    return [[1.0, 0.0] if any(w in t.lower() for w in ("soil", "mulch", "garden"))
            else [0.0, 1.0] for t in texts]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = patch.object(kb, "DATA", self.root / "data")
        self.vault = patch.object(kb, "VAULT", self.root / "vault")
        self.embeddings = patch.object(kb, "embed", side_effect=vectors)
        for p in (self.data, self.vault, self.embeddings):
            p.start()
            self.addCleanup(p.stop)
        kb.VAULT.mkdir()
        kb.init()

    def test_archive_preserves_original_and_user_edits_without_duplicates(self):
        url = "https://example.com/soil"
        with patch.object(kb, "extract", return_value=(
                "Soil care", "Add mulch to your garden. " * 20, b"<original>", ".html", url)) as extractor:
            _, path = kb.ingest(url)
            note = kb.VAULT / path
            note.write_text(note.read_text() + "\nMy own observation.\n")
            kb.ingest(url)
            self.assertEqual(extractor.call_count, 1)
        self.assertIn("My own observation", note.read_text())
        self.assertEqual(list((kb.DATA / "originals").iterdir())[0].read_bytes(), b"<original>")
        with kb.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM search").fetchone()[0], 1)

    def test_failed_embedding_leaves_note_and_retry_indexes_it(self):
        url = "https://example.com/article"
        with patch.object(kb, "extract", return_value=("Title", "A readable article. " * 20, b"raw", ".txt", url)):
            with patch.object(kb, "embed", side_effect=RuntimeError("Model not ready")):
                with self.assertRaises(RuntimeError):
                    kb.ingest(url)
        with patch.object(kb, "extract", side_effect=AssertionError("must use saved note")):
            kb.ingest(url)
        self.assertIn("1 documents", kb.status())

    def test_durable_queue_deduplicates_and_retries_failed_jobs(self):
        url = "https://example.com/one#section"
        self.assertEqual(kb.enqueue(url, 12), "queued")
        self.assertEqual(kb.enqueue(url, 12), "queued")
        kb.init()  # Simulate another process opening the persistent database.
        job = kb.claim_job()
        self.assertEqual(job["url"], "https://example.com/one")
        self.assertIsNone(kb.claim_job())
        kb.finish_job(job["url"], "failed")
        self.assertEqual(kb.enqueue(url, 12), "queued again")
        self.assertIsNotNone(kb.claim_job())
        kb.finish_job(job["url"])
        self.assertEqual(kb.enqueue(url, 12), "done")

    def test_retrieval_keeps_source_mapping(self):
        kb.index_note("soil.md", "Garden soil", "https://example.com/soil", "Mulch helps garden soil.")
        kb.index_note("music.md", "Music", "https://example.com/music", "Music notes and piano.")
        result = kb.retrieve("How does mulch help?")[0]
        self.assertEqual(result["title"], "Garden soil")
        self.assertEqual(result["url"], "https://example.com/soil")
        self.assertEqual(result["path"], "soil.md")
        self.assertEqual(result["text"], "Mulch helps garden soil.")

    def test_edited_and_deleted_notes_update_index(self):
        folder = kb.VAULT / "Knowledge Inbox"
        folder.mkdir()
        note = folder / "local.md"
        note.write_text("# Garden\nMulch and soil")
        self.assertEqual(kb.sync_vault(), 0)
        note.write_text("# Garden\nMulch keeps soil moist")
        kb.sync_vault()
        self.assertIn("moist", kb.retrieve("soil")[0]["text"])
        note.unlink()
        kb.sync_vault()
        self.assertEqual(kb.retrieve("soil"), [])

    def test_model_change_requires_reindex(self):
        kb.index_note("test.md", "Garden", "", "Garden soil")
        with patch.object(kb, "MODEL", "other-model"):
            with self.assertRaisesRegex(ValueError, "Reindex"):
                kb.retrieve("soil")
            kb.index_note("test.md", "Garden", "", "Garden soil")
            self.assertEqual(len(kb.retrieve("soil")), 1)

    def test_embedding_failure_does_not_destroy_old_index(self):
        kb.index_note("test.md", "Garden", "", "Garden soil")
        with patch.object(kb, "embed", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                kb.index_note("test.md", "Garden", "", "New soil text")
        self.assertEqual(kb.retrieve("soil")[0]["text"], "Garden soil")

    def test_sync_skips_symlinks_and_hidden_notes(self):
        (kb.VAULT / ".private").mkdir()
        (kb.VAULT / ".private" / "secret.md").write_text("Do not index")
        outside = self.root / "outside.md"
        outside.write_text("Do not index")
        (kb.VAULT / "escape.md").symlink_to(outside)
        with patch.dict(os.environ, {"INDEX_EXISTING_NOTES": "true"}):
            kb.sync_vault()
        self.assertIn("0 documents", kb.status())


class LinkTests(unittest.TestCase):
    def test_youtube_canonicalization_and_host_validation(self):
        self.assertEqual(kb.canonical("https://youtu.be/abcdefghijk?t=30"),
                         "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertIsNone(kb.youtube_id("https://youtube.com.attacker.example/watch?v=abcdefghijk"))
        for url in ("file:///etc/passwd", "https://user:pass@example.com", "http://example.com:8080"):
            with self.assertRaises(ValueError):
                kb.canonical(url)

    def test_private_dns_addresses_are_rejected(self):
        for address in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "::1"):
            with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", (address, 80))]):
                with self.assertRaisesRegex(ValueError, "private"):
                    kb.public_addresses("example.com", 80)

    def test_embed_checks_response_cardinality(self):
        with patch.object(kb, "post_json", return_value={"embeddings": [[1, 0]]}):
            with self.assertRaises(ValueError):
                kb.embed(["one", "two"])


if __name__ == "__main__":
    unittest.main()
