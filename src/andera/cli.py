from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from andera.agent import EvidenceAgent, create_browser
from andera.env import openai_configured
from andera.planner import create_planner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="andera",
        description="Collect audit evidence from a browser target or local fixture.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Execute one evidence-collection task")
    run.add_argument("task", help="Natural-language evidence task")
    run.add_argument("--url", help="Override target URL or local HTML path")
    run.add_argument("--browser", choices=("fixture", "playwright"), default=None)
    run.add_argument("--planner", choices=("openai", "rule"), default="openai")
    run.add_argument("--out", default="runs", help="Directory for artifacts and result.json")
    run.add_argument("--timeout-ms", type=int, default=None, help="Selector wait timeout")
    run.add_argument(
        "--verbose",
        action="store_true",
        help="Print each trajectory step to stderr as it happens",
    )
    args = parser.parse_args(argv)

    try:
        planner = create_planner(args.planner)
        if args.planner == "openai" and not openai_configured():
            raise RuntimeError("OPENAI_API_KEY is not set")
        browser_name = args.browser or ("playwright" if args.planner == "openai" else "fixture")
        browser = create_browser(browser_name)
        try:
            result = EvidenceAgent(browser, Path(args.out), planner=planner, verbose=args.verbose).run(
                args.task, target_url=args.url, timeout_ms=args.timeout_ms
            )
        finally:
            browser.close()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
