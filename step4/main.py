#!/usr/bin/env python3
"""CLI for Step 4 — test tool registry and execution."""

import asyncio
import json
import sys

from step4.tool import ToolRegistry
from step4.tools.echo import EchoTool


async def main() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    if len(sys.argv) >= 2 and sys.argv[1] == "schemas":
        print(json.dumps(registry.get_definitions(), indent=2, ensure_ascii=False))
        return

    if len(sys.argv) >= 2:
        text = " ".join(sys.argv[1:])
    else:
        text = "Hello from Step 4!"

    r = await registry.execute("echo", text=text)
    status = "[ok]" if not r.is_error else "[error]"
    print(f"{status} {r}")


if __name__ == "__main__":
    asyncio.run(main())
