from __future__ import annotations

import argparse
import asyncio

from localmelo.melo.agent import Agent


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
    parser.add_argument("query", nargs="*", help="Task to execute (direct mode)")
    parser.add_argument("--base-url", default=None, help="LLM API base URL")
    parser.add_argument("--chat-model", default=None, help="Chat model name")
    parser.add_argument("--embed-model", default=None, help="Embedding model name")

    # gateway mode
    parser.add_argument("--serve", action="store_true", help="Run as gateway server")
    parser.add_argument("--host", default=None, help="Gateway listen host")
    parser.add_argument("--port", type=int, default=None, help="Gateway listen port")

    # setup
    parser.add_argument(
        "--reconfigure", action="store_true", help="Re-run setup wizard"
    )
    parser.add_argument(
        "--daemon",
        choices=["install", "uninstall", "status"],
        help="Manage launchd daemon",
    )

    args = parser.parse_args()

    # daemon commands
    if args.daemon:
        from localmelo.support.gateway import daemon

        if args.daemon == "install":
            kwargs: dict = {}
            if args.port is not None:
                kwargs["port"] = args.port
            if args.base_url is not None:
                kwargs["base_url"] = args.base_url
            path = daemon.install(**kwargs)
            print(f"Daemon installed: {path}")
        elif args.daemon == "uninstall":
            if daemon.uninstall():
                print("Daemon uninstalled")
            else:
                print("Daemon not installed")
        elif args.daemon == "status":
            info = daemon.status()
            for k, v in info.items():
                print(f"  {k}: {v}")
        return

    # direct mode (no gateway, original behavior)
    if args.query and not args.serve:
        query = " ".join(args.query)
        agent = Agent(
            base_url=args.base_url,
            chat_model=args.chat_model,
            embed_model=args.embed_model,
        )
        asyncio.run(_run(agent, query))
        return

    # gateway mode: load or create config
    from localmelo.support import config
    from localmelo.support.onboard import run_wizard

    cfg = config.load()

    if args.reconfigure or not cfg.is_configured:
        wizard_cfg = run_wizard()
        if wizard_cfg is None:
            return
        cfg = wizard_cfg

    # apply CLI overrides
    if args.host is not None:
        cfg.gateway.host = args.host
    if args.port is not None:
        cfg.gateway.port = args.port

    # validate before anything else — fail fast with actionable errors
    from localmelo.support.config import ConfigError

    try:
        cfg.validate_or_raise()
    except ConfigError as e:
        print(f"\n  {e}\n", file=__import__("sys").stderr)
        raise SystemExit(1) from e

    # start gateway (with LLM subprocess)
    from localmelo.support.gateway import start_gateway

    print("\n  Starting localmelo gateway ...")
    print(f"  Backend: {cfg.backend}")
    print(f"  Gateway: http://{cfg.gateway.host}:{cfg.gateway.port}")
    print(f"  Web UI:  http://{cfg.gateway.host}:{cfg.gateway.port}/")
    print()

    start_gateway(cfg)


if __name__ == "__main__":
    main()
