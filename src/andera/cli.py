from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from andera.agent import EvidenceAgent, create_browser
from andera.parse import parse_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="andera",
        description="Collect audit evidence from a browser target or local fixture.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Execute one evidence-collection task")
    run.add_argument("task", help="Natural-language evidence task")
    run.add_argument("--url", help="Override target URL or local HTML path")
    run.add_argument("--browser", choices=("fixture", "playwright"), default="fixture")
    run.add_argument("--out", default="runs", help="Directory for artifacts and result.json")
    run.add_argument("--timeout-ms", type=int, default=None, help="Selector wait timeout")
    args = parser.parse_args(argv)

    try:
        task = parse_task(args.task, target_url=args.url, timeout_ms=args.timeout_ms)
        browser = create_browser(args.browser)
        try:
            result = EvidenceAgent(browser, Path(args.out)).run(task)
        finally:
            browser.close()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
