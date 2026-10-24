"""
Print the fault scenarios' checks from results.json: each check that held, and each that did not.

    python checks.py [scenario ...]
"""

import json
import os
import sys

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")


def walk(prefix, value):
    """Yield (path, check, held) for every `checks` dict under value"""
    if not isinstance(value, dict):
        return
    for check, held in value.get("checks", {}).items():
        yield prefix, check, held
    for key, child in value.items():
        if key != "checks":
            yield from walk(f"{prefix}/{key}", child)


def main(names):
    results = json.load(open(RESULTS))
    for name in names or sorted(n for n in results if n.startswith("f_")):
        entry = results[name]
        if not entry["ok"]:
            print(f"ERROR {name}\n{entry['error']}")
            continue
        for path, check, held in walk(name, entry["result"]):
            print(f"{'ok  ' if held else 'FAIL'} {path}: {check}")


if __name__ == "__main__":
    main(sys.argv[1:])
