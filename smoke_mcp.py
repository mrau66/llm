"""Real protocol check, run by deploy.sh after starting the service."""
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    for attempt in range(20):
        try:
            async with streamablehttp_client("http://knowledge:8000/mcp") as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    expected = {"save_link", "link_status", "search_knowledge", "knowledge_status"}
                    assert expected <= {t.name for t in tools.tools}
                    result = await session.call_tool("knowledge_status", {})
                    if result.isError:
                        raise RuntimeError("knowledge_status failed")
                    print("MCP initialize, tools/list, and tools/call passed.")
                    return
        except Exception:
            if attempt == 19:
                raise
            await asyncio.sleep(2)


asyncio.run(main())
