"""Inspect the MCP server with fixture data, or explicitly opt into live Ed reads."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    cwd = Path(__file__).resolve().parent
    command = ["server.py"] if args.live else ["-m", "tests.mock_server"]
    import os
    env = {k: os.environ[k] for k in ("ED_API_TOKEN", "ED_REGION") if k in os.environ} if args.live else {}
    launch = StdioServerParameters(command=sys.executable, args=command, cwd=str(cwd), env=env)
    async with asyncio.timeout(90):
        async with stdio_client(launch) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                if not tools or any(not (t.annotations and t.annotations.readOnlyHint) for t in tools):
                    raise RuntimeError("Unexpected non-read-only tool manifest.")
                print("Available tools:", ", ".join(t.name for t in tools))
                course_id = args.course_id if args.live else (args.course_id or 311)
                query = args.query if args.live else (args.query or "Project 3 input")
                checks = [("list_courses", {})]
                if course_id is not None:
                    checks.extend([("list_categories", {"course_id": course_id}),
                                   ("list_threads", {"course_id": course_id, "scan_limit": 5})])
                    if query:
                        checks.append(("find_clarifications", {
                            "course_id": course_id, "query": query,
                            "scan_limit": 20, "detail_budget": 5, "result_limit": 3,
                        }))
                for name, inputs in checks:
                    result = await session.call_tool(name, inputs)
                    if result.isError:
                        msg = result.content[0].text if result.content else "unknown error"
                        raise RuntimeError(f"{name} failed: {msg}")
                    print(json.dumps({"tool": name, "result": result.structuredContent},
                                     indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use a real Ed API token from the environment.")
    parser.add_argument("--course-id", type=int, help="Read a small sample from this course.")
    parser.add_argument("--query", help="Try clarification search in the selected course.")
    args = parser.parse_args()
    if args.live and args.query and args.course_id is None:
        parser.error("--query with --live requires --course-id")
    try:
        asyncio.run(run(args))
    except ExceptionGroup as group:
        error = group
        while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        parser.exit(1, f"{error}\n")
    except (RuntimeError, TimeoutError) as error:
        parser.exit(1, f"{error or 'MCP smoke test timed out.'}\n")


if __name__ == "__main__":
    main()
