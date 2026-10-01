import csv
import json
from pathlib import Path
import sys

report = json.loads(Path(sys.argv[1]).read_text())
with Path(sys.argv[2]).open(newline='') as stream:
    reader = csv.DictReader(stream)
    rows = list(reader)
    columns = reader.fieldnames
assert type(report.get('rows')) is int and report['rows'] == len(rows)
assert report.get('columns') == columns
assert isinstance(report.get('summary'), str) and report['summary'].strip()
