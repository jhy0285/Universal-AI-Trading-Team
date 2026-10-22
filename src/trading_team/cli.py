from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from trading_team.engine import Desk
from trading_team.metrics import performance
from trading_team.replay import ReplayCase, demo_case, run_replay
from trading_team.settings import Settings
from trading_team.storage import Database


async def execute(args):
    settings = Settings()
    db = Database(settings.database_url)
    db.initialize()
    desk = Desk(settings, db)
    try:
        if args.command == "init":
            desk.activate_config()
            return {"initialized": True, "paper_only": True, "model": settings.public()}
        if args.command == "status":
            return {
                "model": settings.public(),
                "capabilities": desk.capabilities(),
                "performance": performance(db, args.account),
            }
        if args.command == "probe":
            return await desk.probe()
        if args.command == "collect":
            return await desk.collect()
        if args.command == "cycle":
            from trading_team.operations import LeaseManager

            async with LeaseManager(db).hold("cycle:" + args.account):
                return await desk.run_cycle(args.account, args.request_id or uuid.uuid4().hex)
        if args.command == "risk":
            from trading_team.operations import LeaseManager

            async with LeaseManager(db).hold("risk:" + args.account):
                return await desk.run_risk(args.account)
        if args.command in {"demo", "replay"}:
            case = (
                demo_case()
                if args.command == "demo"
                else ReplayCase.model_validate_json(Path(args.file).read_text())
            )
            from trading_team.operations import LeaseManager

            async with LeaseManager(db).hold("replay"):
                return await run_replay(
                    desk,
                    case,
                    args.account or "replay-" + uuid.uuid4().hex[:12],
                    use_model=getattr(args, "use_model", False),
                )
        if args.command == "worker":
            from trading_team.worker import run_worker

            await run_worker(desk)
            return None
        raise ValueError("unknown command")
    finally:
        await desk.close()
        db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Universal AI Trading Team — PAPER ONLY")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "probe", "collect", "worker", "providers"):
        sub.add_parser(name)
    for name in ("status", "cycle", "risk"):
        command = sub.add_parser(name)
        command.add_argument("--account", default="main")
        if name == "cycle":
            command.add_argument("--request-id")
    demo = sub.add_parser("demo")
    demo.add_argument("--account")
    replay = sub.add_parser("replay")
    replay.add_argument("file")
    replay.add_argument("--account")
    replay.add_argument(
        "--use-model",
        action="store_true",
        help="Use the one configured live model sequentially, instead of recorded model outputs",
    )
    fixture = sub.add_parser("fixture")
    fixture.add_argument("--output", default="-")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "providers":
        from trading_team.cli_registry import inventory, transport_info

        settings = Settings()
        print(
            json.dumps(
                {
                    "active": settings.public(),
                    "transport": transport_info(settings),
                    "cli_profiles": inventory(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "trading_team.api:app_factory", factory=True, host=args.host, port=args.port, workers=1
        )
        return
    if args.command == "fixture":
        body = demo_case().model_dump_json(indent=2)
        if args.output == "-":
            print(body)
        else:
            Path(args.output).write_text(body + "\n")
        return
    result = asyncio.run(execute(args))
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
