"""
Build the local dashboard HTML by inlining the current snapshot.json
into the `const data = {…};` literal in kalshi_dashboard.html.

Also refreshes the "Generated: …" timestamp in the header.

Usage:
    python3 build_dashboard.py
    python3 build_dashboard.py --template path/to/template.html --output path/to/built.html

By default reads:
    template:  ./templates/kalshi_dashboard.html
    snapshot:  ./data/snapshot.json
    output:    ./data/kalshi_dashboard.html

You can also pipe the export → build into one command:
    python3 kalshi_export.py --write && python3 build_dashboard.py
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TEMPLATE = os.path.join(HERE, "templates", "kalshi_dashboard.html")
DEFAULT_SNAPSHOT = os.path.join(HERE, "data", "snapshot.json")
DEFAULT_OUTPUT = os.path.join(HERE, "data", "kalshi_dashboard.html")

# Matches:  const data = {...anything that isn't a closing-};-on-its-own...};
# Non-greedy, multi-line. Anchored on the start of a line so we don't accidentally
# match a similar pattern that might appear inside a string literal elsewhere.
DATA_PATTERN = re.compile(
    r"^const data = \{.*?\};\s*$",
    re.MULTILINE | re.DOTALL,
)

# Matches:  Generated: YYYY-MM-DD HH:MM UTC  (inside the header span)
TIMESTAMP_PATTERN = re.compile(
    r"Generated:\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+UTC"
)


def build(template_path, snapshot_path, output_path):
    if not os.path.exists(template_path):
        print(f"ERROR: template not found: {template_path}", file=sys.stderr)
        return 1
    if not os.path.exists(snapshot_path):
        print(f"ERROR: snapshot not found: {snapshot_path}", file=sys.stderr)
        return 1

    with open(template_path, "r") as f:
        html = f.read()

    with open(snapshot_path, "r") as f:
        snapshot = json.load(f)

    # Replace the data literal. Serialize compactly so the file stays small.
    data_json = json.dumps(snapshot, separators=(",", ":"))
    new_data_line = f"const data = {data_json};"

    # re.subn interprets backslash escapes (\1, \u, etc.) in the replacement
    # string. Our JSON contains \u escapes — pass via a lambda to bypass that.
    new_html, n_replaced = DATA_PATTERN.subn(lambda _: new_data_line, html, count=1)
    if n_replaced == 0:
        print("ERROR: could not find 'const data = {...};' in template.", file=sys.stderr)
        print("       The template's data block doesn't match the expected pattern.", file=sys.stderr)
        return 2

    # Refresh the "Generated:" timestamp in the header. Prefer the snapshot's
    # own generated_at over the current wall clock — that's what the data
    # actually reflects.
    try:
        gen_dt = datetime.fromisoformat(snapshot["generated_at"].replace("Z", "+00:00"))
        gen_dt_utc = gen_dt.astimezone(timezone.utc)
    except Exception:
        gen_dt_utc = datetime.now(timezone.utc)
    ts_str = gen_dt_utc.strftime("Generated: %Y-%m-%d %H:%M UTC")

    new_html, n_ts = TIMESTAMP_PATTERN.subn(ts_str, new_html, count=1)
    if n_ts == 0:
        print("WARNING: 'Generated: …' timestamp not found in template — left as-is.",
              file=sys.stderr)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(new_html)

    # Report what got written
    n_markets = len(snapshot.get("markets", {}))
    n_accuracy = len(snapshot.get("accuracy", {}) or {})
    snap_size = os.path.getsize(snapshot_path)
    html_size = os.path.getsize(output_path)
    print(f"Built {output_path}")
    print(f"  template:    {template_path}")
    print(f"  snapshot:    {snapshot_path}  ({snap_size:,} bytes)")
    print(f"  generated:   {ts_str.replace('Generated: ', '')}")
    print(f"  markets:     {n_markets}")
    print(f"  accuracy:    {n_accuracy}")
    print(f"  output size: {html_size:,} bytes")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Inline snapshot.json into the dashboard HTML.")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help=f"path to dashboard HTML template (default: {DEFAULT_TEMPLATE})")
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                        help=f"path to snapshot.json (default: {DEFAULT_SNAPSHOT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"output path (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    template = os.path.expanduser(args.template)
    snapshot = os.path.expanduser(args.snapshot)
    output = os.path.expanduser(args.output)

    return build(template, snapshot, output)


if __name__ == "__main__":
    sys.exit(main())
