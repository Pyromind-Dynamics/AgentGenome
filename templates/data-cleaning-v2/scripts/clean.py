import csv
from pathlib import Path
import sys

source, delimiter = Path(sys.argv[1]), sys.argv[2]
if source.suffix != '.csv':
    raise ValueError('input filename must end with .csv')
with source.open(newline='', encoding='utf-8') as stream:
    reader = csv.DictReader(stream, delimiter=delimiter)
    if not reader.fieldnames or len(reader.fieldnames) < 2:
        raise ValueError('expected at least two columns; check delimiter')
    writer = csv.DictWriter(sys.stdout, fieldnames=reader.fieldnames)
    writer.writeheader()
    seen = set()
    for row in reader:
        values = tuple(row.get(key, '') for key in reader.fieldnames)
        if all(isinstance(value, str) and value.strip() for value in values) and values not in seen:
            seen.add(values)
            writer.writerow(row)
