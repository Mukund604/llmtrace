"""llmtrace report traces.db | llmtrace drift traces.db --hours 24"""
import argparse
import json
import sys
import time

from .analysis import drift, llm_rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(prog="llmtrace", description=__doc__)
    parser.add_argument("command", choices=["report", "drift"])
    parser.add_argument("path")
    parser.add_argument("--hours", type=float, default=24, help="size of the current window for drift")
    args = parser.parse_args(argv)
    if args.command == "report":
        print(json.dumps(summary(llm_rows(args.path)), indent=2))
        return 0
    cut = time.time() - args.hours * 3600
    alerts = drift(llm_rows(args.path, until=cut), llm_rows(args.path, since=cut))
    print(json.dumps(alerts, indent=2))
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
