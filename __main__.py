from __future__ import annotations

import argparse
import asyncio

from .core import Agent


async def _run(agent: Agent, query: str | None) -> None:
    try:
        if query:
            result = await agent.run(query)
            print(result)
        else:
            print("melo agent (type 'exit' to quit)")
            while True:
                try:
                    q = input("\n> ")
                except (EOFError, KeyboardInterrupt):
                    break
                if q.strip().lower() in ("exit", "quit"):
                    break
                if not q.strip():
                    continue
                result = await agent.run(q)
                print(f"\n{result}")
    finally:
        await agent.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="melo", description="localmelo agent")
    parser.add_argument("query", nargs="*", help="Task to execute")
    parser.add_argument("--base-url", default=None, help="LLM API base URL")
    parser.add_argument("--chat-model", default=None, help="Chat model name")
    parser.add_argument("--embed-model", default=None, help="Embedding model name")
    args = parser.parse_args()

    query = " ".join(args.query) if args.query else None

    agent = Agent(
        base_url=args.base_url,
        chat_model=args.chat_model,
        embed_model=args.embed_model,
    )
    asyncio.run(_run(agent, query))


if __name__ == "__main__":
    main()
