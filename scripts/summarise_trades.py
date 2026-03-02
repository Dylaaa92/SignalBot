#!/usr/bin/env python3
import json, glob, datetime as dt

SYMBOL = "ETH"
DAYS = 4
DIR = "trades"

today = dt.date.today()
start = today - dt.timedelta(days=DAYS-1)

files = sorted(glob.glob(f"{DIR}/{SYMBOL}-*.jsonl"))

def in_range(path):
    try:
        y,m,d = path.split("-")[-3:]
        d = d.replace(".jsonl","")
        file_date = dt.date(int(y),int(m),int(d))
        return start <= file_date <= today
    except:
        return False

files = [f for f in files if in_range(f)]

print(f"Scanning {len(files)} files from {start} to {today}\n")

for f in files:
    print("FILE:", f)
    with open(f) as fh:
        for line in fh:
            try:
                j = json.loads(line)
            except:
                continue
            typ = j.get("type") or j.get("event")
            if typ and typ.upper() not in ("HEARTBEAT",):
                print(j)
