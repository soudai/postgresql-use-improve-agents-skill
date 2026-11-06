"""Findings and restricted output for PostgreSQL security reviews."""
import html
import json
import os
from pathlib import Path


def write_new(path: Path, content: str):
    # Exclusive creation and owner-only mode on Unix. Use a private directory on Windows.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


class Review:
    def __init__(self, snapshot, contract):
        self.snapshot = snapshot
        self.sections = snapshot["sections"]
        self.contract = contract
        self.findings = {}
        self.scope = {
            "target": snapshot["target"],
            "database": snapshot["identity"]["database"],
            "collected_at": snapshot["collected_at"],
        }

    def rows(self, name):
        section = self.sections.get(name, {})
        return section.get("rows", []) if section.get("status") == "ok" else []

    def ready(self, *names):
        return all(self.sections.get(name, {}).get("status") == "ok" for name in names)

    def add(self, rule, subject, status, severity, evidence, expected, improvement, verify, restore,
            confidence="observed"):
        key = json.dumps([rule, self.scope["target"], self.scope["database"], subject],
                         ensure_ascii=False, separators=(",", ":"))
        self.findings[key] = {
            "id": key, "rule": rule, "subject": subject, "status": status, "severity": severity,
            "confidence": confidence, "evidence": evidence, "expected": expected,
            "improvement": improvement, "verify": verify, "restore": restore,
        }

    def missing(self, rule, subject, expected, evidence="Required evidence is missing or incomplete."):
        self.add(rule, subject, "unknown", "unrated", evidence, expected,
                 "Collect scoped metadata or confirm the access contract.",
                 "Repeat the review for the same target.", "No change proposed.", confidence="unverified")


def render(report, format_name):
    if format_name == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if format_name == "issue":
        # External drafts only contain fixed rule IDs and counts. No identifiers or raw evidence.
        counts = {}
        for f in report["findings"]:
            if f["status"] in {"finding", "unknown"}:
                key = (f["rule"], f["status"], f["severity"])
                counts[key] = counts.get(key, 0) + 1
        lines = ["# PostgreSQL security review follow-up", "",
                 "Review private evidence, confirm exposure and agree on change and verification scope.", "",
                 "| Rule | Status | Severity | Count |", "| --- | --- | --- | --- |"]
        lines.extend(f"| {rule} | {status} | {severity} | {count} |"
                     for (rule, status, severity), count in sorted(counts.items()))
        return "\n".join(lines) + "\n"
    def esc(value):
        value = html.escape(str(value)).replace("\r", " ").replace("\n", " ")
        for char in "\\\x60*_{}[]|":
            value = value.replace(char, "\\" + char)
        return value
    lines = ["# PostgreSQL security review", "",
             "- Target " + esc(report["scope"]["target"]),
             "- Database " + esc(report["scope"]["database"]),
             "- Collected " + esc(report["scope"]["collected_at"]), "",
             "## Coverage", ""]
    lines.extend(f"- {esc(k)} · {esc(v)}" for k, v in sorted(report["coverage"].items()))
    lines.extend(["", "## Findings", ""])
    for f in report["findings"]:
        lines.extend([f"### {esc(f['rule'])} · {esc(f['status'])} · {esc(f['severity'])}", "",
                      f"- Subject · {esc(f['subject'])}", f"- Confidence · {esc(f['confidence'])}",
                      f"- Evidence · {esc(f['evidence'])}", f"- Expected · {esc(f['expected'])}",
                      f"- Improve · {esc(f['improvement'])}", f"- Verify · {esc(f['verify'])}",
                      f"- Recover · {esc(f['restore'])}", f"- Source · {esc(f['source'])}"])
        if f.get("exception"):
            lines.append(f"- Exception · {esc(f['exception'])}")
        lines.append("")
    if "comparison" in report:
        lines.extend(["## Previous review", ""])
        lines.extend(f"- {esc(k)} · {esc(v)}" for k, v in report["comparison"].items())
    return "\n".join(lines) + "\n"
