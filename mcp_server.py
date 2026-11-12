import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from service import KnowledgeService

logging.basicConfig(level=logging.INFO)
service = KnowledgeService()


@asynccontextmanager
async def lifespan(server):
    await service.start()
    try:
        yield {}
    finally:
        await service.stop()


mcp = FastMCP("Personal knowledge base", host="0.0.0.0", port=8000,
              stateless_http=True, json_response=True, lifespan=lifespan,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=True,
                  allowed_hosts=["knowledge:8000", "localhost:8000", "127.0.0.1:8000"]))


@mcp.tool()
async def save_link(url: str) -> dict:
    """Archive a user-shared article/PDF/YouTube URL into Obsidian and local RAG.

    Returns a durable queue acknowledgement. Use link_status to check completion.
    """
    return await service.save_link(url)


@mcp.tool()
async def link_status(url: str) -> dict:
    """Check whether a queued link is indexed, still processing, or failed."""
    return await service.link_status(url)


@mcp.tool()
async def search_knowledge(query: str, limit: int = 6) -> dict:
    """Search all indexed Obsidian knowledge using natural language.

    Returns source passages for ZeroClaw to answer from with citations.
    """
    return await service.search(query, limit)


@mcp.tool()
async def knowledge_status() -> str:
    """Count archived notes, indexed passages, and queued/failed ingestion jobs."""
    return await service.status()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
