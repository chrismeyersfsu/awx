"""Print a live scenario's result compactly: ansible_ids shortened, service ids named"""

import json
import re
import sys

r = json.load(open("results.json"))
JEWEL_SID = None
for v in r.values():
    if v["ok"]:
        s = json.dumps(v["result"])
        m = re.search(r'"admin": \[1, \["[0-9a-f-]+", "([0-9a-f-]+)"', s)
        if m:
            JEWEL_SID = m.group(1)
            break


def short(text):
    text = text.replace(JEWEL_SID, "JEWEL") if JEWEL_SID else text
    return re.sub(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r"\1", text)


for name in sys.argv[1:]:
    v = r[name]
    print(f"===== {name} ({v.get('suffix')})")
    for key, value in v["result"].items():
        print(f"  {key}: {short(json.dumps(value, default=str))[:1400]}")
