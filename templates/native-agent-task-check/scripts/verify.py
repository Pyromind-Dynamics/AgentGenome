"""Compare the Agent's report to the actual CSV; no model judge is involved."""
import csv
import json
import sys


def main():
    with open(sys.argv[1], encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with open(sys.argv[2], encoding="utf-8") as stream:
        report = json.load(stream)
    names = [row["name"] for row in rows]
    if not isinstance(report, dict):
        raise ValueError("report must be a JSON object")
    if type(report.get("rows")) is not int or report["rows"] != len(rows):
        raise ValueError("report.rows does not match the CSV")
    if report.get("names") != names:
        raise ValueError("report.names does not match the CSV order")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("report.summary must be nonempty text")
    print(json.dumps({"passed": True, "rows": len(rows), "names": names}, ensure_ascii=False))


if __name__ == "__main__":
    main()
