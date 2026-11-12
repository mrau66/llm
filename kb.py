"""Local archive, durable ingestion queue, and hybrid retrieval."""
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


VAULT = Path(os.getenv("VAULT_PATH", "/vault"))
DATA = Path(os.getenv("DATA_PATH", "/data"))
MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
OLLAMA = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MAX_BYTES = 15 * 1024 * 1024


def youtube_id(url):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    value = None
    if host == "youtu.be":
        value = p.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if p.path == "/watch":
            value = parse_qs(p.query).get("v", [""])[0]
        elif p.path.startswith(("/shorts/", "/live/", "/embed/")):
            value = p.path.split("/")[2]
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def canonical(url):
    p = urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Use an http(s) URL without embedded credentials.")
    if p.port not in {None, 80, 443}:
        raise ValueError("Only standard web ports are supported.")
    video = youtube_id(url)
    if video:
        return "https://www.youtube.com/watch?v=" + video
    # Keep query semantics (including signed URLs), remove only the fragment.
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def public_addresses(host, port):
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError("Links to private or local network addresses are not allowed.")
    return addresses


def fetch(url):
    """Fetch bounded public HTTP content, pinning DNS results for each redirect."""
    for _ in range(6):
        url = canonical(url)
        p = urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        ip = public_addresses(p.hostname, port)[0]
        conn = http.client.HTTPConnection(p.hostname, port, timeout=25)
        sock = socket.create_connection((ip, port), timeout=25)
        if p.scheme == "https":
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
            except Exception:
                sock.close()
                raise
        conn.sock = sock
        try:
            conn.request("GET", urlunsplit(("", "", p.path, p.query, "")), headers={
                "User-Agent": "PersonalKnowledgeArchive/1.0",
                "Accept-Encoding": "identity",
            })
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect has no destination.")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Site returned HTTP {response.status}; it may require a login.")
            if int(response.getheader("Content-Length", "0")) > MAX_BYTES:
                raise ValueError("Document exceeds the 15 MB download limit.")
            deadline = time.monotonic() + 90
            parts, size = [], 0
            while True:
                part = response.read(min(65536, MAX_BYTES + 1 - size))
                if not part:
                    break
                parts.append(part)
                size += len(part)
                if size > MAX_BYTES or time.monotonic() > deadline:
                    raise ValueError("Document exceeds the download size or time limit.")
            return b"".join(parts), response.getheader("Content-Type", ""), url
        finally:
            conn.close()
    raise ValueError("Too many redirects.")


def extract(url):
    video = youtube_id(url)
    if video:
        from youtube_transcript_api import YouTubeTranscriptApi
        languages = os.getenv("TRANSCRIPT_LANGUAGES", "en").split(",")
        transcript = YouTubeTranscriptApi().fetch(video, languages=languages)
        snippets = list(transcript)
        text = "\n".join(f"[{int(s.start)//60:02d}:{int(s.start)%60:02d}] {s.text}"
                         for s in snippets)
        title = f"YouTube {video}"
        try:
            raw, _, _ = fetch("https://www.youtube.com/oembed?" + urlencode({
                "url": url, "format": "json"}))
            title = json.loads(raw)["title"]
        except Exception:
            pass  # Captions remain useful even when optional title metadata fails.
        original = json.dumps([{"start": s.start, "duration": s.duration, "text": s.text}
                               for s in snippets], ensure_ascii=False).encode()
        return title, text, original, ".transcript.json", url
    raw, mime, final = fetch(url)
    if "application/pdf" in mime or raw.startswith(b"%PDF-"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) > 300:
            raise ValueError("PDF exceeds the 300-page limit.")
        text = "\n\n".join(f"[Page {i+1}]\n{page.extract_text() or ''}"
                             for i, page in enumerate(reader.pages))
        title = str((reader.metadata or {}).get("/Title") or Path(urlsplit(final).path).name)
        return title, text, raw, ".pdf", final
    if "html" in mime or raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        import trafilatura
        result = trafilatura.bare_extraction(raw, url=final, include_comments=False,
                                             include_tables=True, with_metadata=True)
        if result is None:
            raise ValueError("No readable article found; the site may require JavaScript or a login.")
        return result.title or final, result.text or "", raw, ".html", final
    if mime.startswith("text/plain") or mime.startswith("text/markdown"):
        return Path(urlsplit(final).path).name or final, raw.decode("utf-8", "replace"), raw, ".txt", final
    raise ValueError("Supported links: web articles, text, PDFs, and YouTube captions.")


def post_json(url, payload, headers=None):
    request = Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=300) as response:
        return json.load(response)


def embed(texts, query=False):
    prefix = "search_query: " if query else "search_document: "
    vectors = []
    for start in range(0, len(texts), 16):
        result = post_json(OLLAMA + "/api/embed", {
            "model": MODEL, "input": [prefix + t for t in texts[start:start+16]],
            "truncate": False,
        })["embeddings"]
        vectors.extend(result)
    if len(vectors) != len(texts) or any(not v or not all(math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("Embedding service returned invalid vectors.")
    return vectors


def chunks(text, size=1800, overlap=250):
    text = text.strip()
    return [text[i:i+size] for i in range(0, len(text), size-overlap)]


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "knowledge.sqlite", timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        with con:
            yield con
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
          path TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
          digest TEXT NOT NULL, model TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY, path TEXT NOT NULL, text TEXT NOT NULL, vector TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text);
        CREATE TABLE IF NOT EXISTS jobs (
          url TEXT PRIMARY KEY, channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
          updated REAL NOT NULL);
        """)


def index_note(path, title, url, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    with connect() as con:
        old = con.execute("SELECT digest,model FROM documents WHERE path=?", (path,)).fetchone()
    if old and old["digest"] == digest and old["model"] == MODEL:
        return False
    pieces = chunks(text)
    vectors = embed(pieces)
    with connect() as con:
        con.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (path,))
        con.execute("DELETE FROM chunks WHERE path=?", (path,))
        con.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)", (path, title, url, digest, MODEL))
        for piece, vector in zip(pieces, vectors):
            cur = con.execute("INSERT INTO chunks(path,text,vector) VALUES (?,?,?)",
                              (path, piece, json.dumps(vector)))
            con.execute("INSERT INTO search(rowid,text) VALUES (?,?)", (cur.lastrowid, piece))
    return True


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def ingest(url):
    url = canonical(url)
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    relative = f"Knowledge Inbox/{key}.md"
    path = VAULT / relative
    if path.exists():
        # Preserve user edits if a job retries after an indexing failure.
        text = path.read_text()
        title = next((x[2:] for x in text.splitlines() if x.startswith("# ")), url)
    else:
        title, body, raw, suffix, final = extract(url)
        if len(body.strip()) < 100:
            raise ValueError("Too little readable text. Scanned PDFs need OCR; videos need captions.")
        if len(body) > 1_000_000:
            raise ValueError("Extracted text exceeds the one-million-character limit.")
        title = " ".join(title.split())[:300]
        timestamp = datetime.now(timezone.utc).isoformat()
        text = (f"---\nsource: {json.dumps(url)}\nresolved_source: {json.dumps(final)}\n"
                f"captured: {json.dumps(timestamp)}\ntags: [knowledge-inbox]\n---\n\n"
                f"# {title}\n\nSource: <{url}>\n\n{body}\n")
        atomic_write(DATA / "originals" / (key + suffix), raw)
        atomic_write(path, text.encode())
    index_note(relative, title, url, text)
    return title, relative


def enqueue(url, channel):
    url = canonical(url)
    with connect() as con:
        cur = con.execute("INSERT OR IGNORE INTO jobs(url,channel,updated) VALUES (?,?,?)",
                          (url, str(channel), time.time()))
        if cur.rowcount:
            return "queued"
        row = con.execute("SELECT status FROM jobs WHERE url=?", (url,)).fetchone()
        if row["status"] == "failed":
            con.execute("UPDATE jobs SET status='queued',attempts=0,error='',channel=?,updated=? WHERE url=?",
                        (str(channel), time.time(), url))
            return "queued again"
        return row["status"]


def claim_job():
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY updated LIMIT 1").fetchone()
        if row:
            con.execute("UPDATE jobs SET status='processing',attempts=attempts+1,updated=? WHERE url=?",
                        (time.time(), row["url"]))
        return dict(row) if row else None


def finish_job(url, error=""):
    with connect() as con:
        # Error detail stays in logs; only a bounded diagnostic is persisted.
        con.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE url=?",
                    ("failed" if error else "done", error[:500], time.time(), url))


def sync_vault():
    root = VAULT if os.getenv("INDEX_EXISTING_NOTES", "false").lower() == "true" else VAULT / "Knowledge Inbox"
    failures = 0
    for path in root.rglob("*.md"):
        relative = path.relative_to(VAULT).as_posix()
        if path.is_symlink() or any(p.startswith(".") for p in Path(relative).parts):
            continue
        if not path.resolve().is_relative_to(VAULT.resolve()) or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            title = next((s[2:] for s in text.splitlines() if s.startswith("# ")), path.stem)
            source = re.search(r'^source: (".*")$', text, re.MULTILINE)
            url = json.loads(source[1]) if source else ""
            index_note(relative, title, url, text)
        except Exception:
            failures += 1
    with connect() as con:
        for row in con.execute("SELECT path FROM documents").fetchall():
            if not (VAULT / row["path"]).is_file():
                con.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (row["path"],))
                con.execute("DELETE FROM chunks WHERE path=?", (row["path"],))
                con.execute("DELETE FROM documents WHERE path=?", (row["path"],))
    return failures


def retrieve(question, limit=8):
    with connect() as con:
        if con.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0:
            return []
        if con.execute("SELECT 1 FROM documents WHERE model != ? LIMIT 1", (MODEL,)).fetchone():
            raise ValueError("Embedding model changed. Reindex all notes before asking questions.")
        rows = con.execute("SELECT c.*,d.title,d.url FROM chunks c JOIN documents d USING(path)").fetchall()
        tokens = re.findall(r"\w+", question)[:30]
        expression = " OR ".join('"' + t + '"' for t in tokens)
        lexical = con.execute("SELECT rowid FROM search WHERE search MATCH ? ORDER BY rank LIMIT 40",
                              (expression,)).fetchall() if expression else []
    query = embed([question], query=True)[0]
    norm = math.sqrt(sum(x*x for x in query)) or 1
    semantic = []
    for row in rows:
        vector = json.loads(row["vector"])
        if len(vector) != len(query):
            raise ValueError("Embedding dimensions changed. Reindex the vault.")
        score = sum(x*y for x, y in zip(query, vector)) / (norm * (math.sqrt(sum(x*x for x in vector)) or 1))
        semantic.append((score, row["id"]))
    scores = {}
    for ranking in [[r[1] for r in sorted(semantic, reverse=True)[:40]], [r[0] for r in lexical]]:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    lookup = {r["id"]: dict(r) for r in rows}
    return [lookup[key] for key in sorted(scores, key=scores.get, reverse=True)[:limit]]


def status():
    with connect() as con:
        docs = con.execute("SELECT count(*) FROM documents").fetchone()[0]
        pieces = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        jobs = dict(con.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
    return f"{docs} documents, {pieces} passages. Ingestion jobs: {jobs}"
