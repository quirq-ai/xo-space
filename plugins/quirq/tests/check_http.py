"""Optional smoke check against the local demo server on port 5004."""
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://127.0.0.1:5004/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 6
            result = await session.call_tool("space_dashboard", {})
            assert result.structuredContent["demo"] is True
            resource = await session.read_resource("ui://xo-space/dashboard")
            assert resource.contents[0].mimeType == "text/html;profile=mcp-app"
            print("HTTP checks passed: initialize, list tools, dashboard result, UI resource.")


if __name__ == "__main__":
    asyncio.run(main())
