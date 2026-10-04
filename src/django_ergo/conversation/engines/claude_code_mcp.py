"""An MCP server that lists Ergo's tools to Claude Code and runs none of them.

The Claude Code engine starts this with a JSON file of tool definitions.
Claude Code sees the tools and the model can call them, but the CLI runs in
``dontAsk`` mode, so it denies every call and Ergo runs the tools itself.
A call that still reaches this server gets an error back.

Standard library only: it runs as a plain script, outside Django.
"""

import json
import sys
from pathlib import Path


def main() -> None:
    tools = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        result: dict = {}
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ergo", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            result = {
                "isError": True,
                "content": [{"type": "text", "text": "Ergo runs this tool itself."}],
            }
        if "id" in request:
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
