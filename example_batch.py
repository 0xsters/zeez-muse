"""Multi-account batch registration/redemption example.

Usage: python example_batch.py [count]
Reads config.json (see the batch fields in config.example.json) and runs with batch scheduling semantics,
then prints the batch_summary JSON at the end. For personal learning and research only.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musepy.runner import BatchRunner


def load_config(path="config.json"):
    if not os.path.exists(path):
        raise SystemExit(f"Missing config file {path}; copy config.example.json and fill it in")
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    runner = BatchRunner(load_config())
    try:
        queued = runner.init(count)
    except ValueError as exc:
        raise SystemExit(f"Incomplete config: {exc}")
    print(f"[i] queued={queued} workers={runner.workers} "
          f"max_per_ip={runner.max_per_ip}", flush=True)
    runner.run()
    print(json.dumps(runner.summary(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
