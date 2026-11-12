# Obsidian knowledge tools for ZeroClaw

A Docker service that gives an existing ZeroClaw agent four MCP tools:

- `save_link`: queue a public article, PDF, text document, or YouTube caption archive.
- `link_status`: check whether extraction and indexing completed.
- `search_knowledge`: retrieve relevant passages using local embeddings and keyword search.
- `knowledge_status`: inspect archive/index/queue counts.

Markdown notes are written to an existing Obsidian vault. Original downloads and the SQLite index stay in a separate local data directory. ZeroClaw uses its existing AI provider to answer from retrieved passages; this service does not connect to Discord or generate answers itself. Retrieved excerpts follow ZeroClaw's existing provider privacy boundary.

## Container image

The workflow publishes **private** images to `ghcr.io/mrau66/personal-knowledge:latest` and `ghcr.io/mrau66/personal-knowledge:sha-COMMIT`. This source repository is public; the package must remain private. Check the Actions run before assuming the image exists. Publication tests the application logic, builds the actual image, and checks a running MCP server before uploading. It also checks package visibility.

For private pulls, configure `ghcr.io` credentials in Dockhand using username `mrau66` and a GitHub personal access token (classic) with `read:packages`. Keep tokens out of YAML and chat. GitHub Actions uses its own built-in token to publish.

## Dockhand

Use `dockhand-private-compose.yaml` after the first successful publication. Adjust the vault and data paths for your host. Create the external Docker network `zeroclaw-knowledge` before deployment. The stack includes Ollama, an embedding-model initializer, a data-directory initializer, and the knowledge MCP server. Init containers exiting with code 0 is expected.

Attach the existing ZeroClaw container to that network (see `zeroclaw-network.override.yaml`). On the host, run `connect-zeroclaw.sh` from a copy of this repository. It backs up the existing ZeroClaw config and workspace instructions, adds the MCP endpoint, and restarts ZeroClaw. It supports legacy and aliased-agent config layouts; select `ZEROCLAW_AGENT` or `ZEROCLAW_WORKSPACE` when automatic selection is ambiguous. `KNOWLEDGE_CONTAINER` can override the knowledge container name.

The instruction file tells ZeroClaw to archive bare links or explicit save requests and search saved content when asked. Existing channel permissions, mention requirements, and tool approvals apply. The backend does not send completion notifications independently.

## Limits

YouTube videos require available captions in configured languages. This does not perform OCR, transcribe uncaptained video, execute JavaScript, or bypass login/paywall barriers. Extraction requests may be blocked by source sites. Full video/audio and embedded images are not archived. Vector search scans SQLite vectors; only one service replica is supported.

The service indexes `Knowledge Inbox` by default. Set `INDEX_EXISTING_NOTES=true` to include other Markdown notes in the vault. Edits/deletions are synced every five minutes. Deleting a note removes it from search, but original downloads and job records remain. Back up the vault and data directory separately; a registry image contains no runtime data.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Unit tests mock external content extraction and embeddings. The CI workflow additionally checks the installed MCP SDK in a real container. Model downloads, real extraction, and actual ZeroClaw tool use should be verified in your deployment.

[GitHub Container Registry authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
