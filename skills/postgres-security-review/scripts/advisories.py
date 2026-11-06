#!/usr/bin/env python3
"""Refresh an offline evidence file from PostgreSQL's public release and security tables."""
import argparse
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen

from report import write_new

RELEASES = "https://www.postgresql.org/support/versioning/"
SECURITY = "https://www.postgresql.org/support/security/"


class Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag == "td" and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def rows(page):
    parser = Rows()
    parser.feed(page)
    return parser.rows


def split_version(value):
    parts = value.split(".")
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        raise ValueError("unrecognized release version")
    if len(parts) != (2 if int(parts[0]) >= 10 else 3):
        raise ValueError("unrecognized release version")
    return ".".join(parts[:-1]), int(parts[-1])


def parse(release_page, security_page, now):
    releases = {}
    for row in rows(release_page):
        if len(row) < 5 or not re.fullmatch(r"\d+(?:\.\d+)?", row[0]):
            continue
        major, minor = split_version(row[1])
        if major != row[0]:
            raise ValueError("inconsistent release table")
        releases[major] = {"minor": minor, "end": datetime.strptime(row[4], "%B %d, %Y").date().isoformat()}
    advisories = []
    for row in rows(security_page):
        match = re.match(r"CVE-\d{4}-\d{4,}", row[0])
        if match is None:
            continue
        if len(row) < 5:
            raise ValueError("unrecognized security table")
        component = row[3].split()
        if not component:
            raise ValueError("missing advisory component")
        fixed = dict(split_version(item) for item in re.findall(r"\b\d+(?:\.\d+){1,2}\b", row[2]))
        advisories.append({"cve": match[0], "component": "server" if row[3].startswith("core server") else
                           "client" if row[3].startswith("client") else "other", "fixed": fixed})
    if not releases or not any(a["component"] == "server" and a["fixed"] for a in advisories):
        raise ValueError("release or advisory table missing")
    return {"schema_version": 1, "source": "postgresql.org", "retrieved_at": now.isoformat(),
            "urls": [RELEASES, SECURITY], "releases": releases, "advisories": advisories}


def fetch(url):
    with urlopen(url, timeout=15) as response:
        if response.url != url:
            raise ValueError("unexpected redirect")
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("response too large")
    return data.decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = parse(fetch(RELEASES), fetch(SECURITY), datetime.now(timezone.utc))
        write_new(args.output, json.dumps(result, indent=2) + "\n")
    except (OSError, ValueError):
        parser.exit(2, "Could not refresh official advisory evidence. Keep existing evidence and report freshness.\n")


if __name__ == "__main__":
    main()
