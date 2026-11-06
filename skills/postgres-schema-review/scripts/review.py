#!/usr/bin/env python3
"""Review a catalog snapshot with explicit design contracts and scoped observations."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import html
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

SECTIONS = ("tables", "columns", "constraints", "indexes", "statistics")
RULES = ("schema.fk", "schema.naming", "schema.null", "schema.flag",
         "schema.lifecycle", "schema.responsibility", "schema.immutable", "schema.contention")
MIGRATION = [
    "依存するSQL・ORM・API・BI/ETLと行の多重度、原子性、保存/訂正要件を確認する。",
    "既存データの違反を確認し、バックフィルと旧新アプリの併存・書込みの正本を定義する。",
    "変更のロック/停止影響、権限・RLS・索引・制約の引継ぎを対象版で確認する。",
    "結果の意味的な一致、許可/拒否、代表負荷の待機時間・p95/p99・失敗/再試行を比較する。",
    "段階的な切替、停止条件、切戻し時の新側書込みの扱いを決める。",
]


class InputError(ValueError):
    pass


def require(condition, path, expected):
    if not condition:
        raise InputError(path + ": " + expected)


def obj(value, path):
    require(isinstance(value, dict), path, "expected object")


def arr(value, path):
    require(isinstance(value, list), path, "expected array")


def string(value, path):
    require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value,
            path, "expected nonempty string")


def strings(value, path, nonempty=False):
    arr(value, path)
    require(not nonempty or bool(value), path, "expected nonempty array")
    for item in value:
        string(item, path)
    require(len(value) == len(set(value)), path, "duplicate entries")


def boolean(value, path):
    require(type(value) is bool, path, "expected boolean")


def number(value, path):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    require(valid, path, "expected finite nonnegative number")


def timestamp(value, path):
    string(value, path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, path, "timestamp must include timezone")
    except ValueError:
        raise InputError(path + ": expected ISO8601 timestamp with timezone") from None


def fields(item, path, allowed):
    obj(item, path)
    require(not set(item) - set(allowed), path, "unknown field (check input format)")


def optional(item, name, check, path):
    if name in item:
        check(item[name], path + "." + name)


def choice(value, path, allowed):
    require(isinstance(value, str) and value in allowed, path, "unsupported value")


def document(value, name, versions=(1,)):
    obj(value, name)
    require(type(value.get("schema_version")) is int and value["schema_version"] in versions,
            name + ".schema_version", "unsupported version; expected " + "/".join(map(str,versions)))


def validate_snapshot(snapshot):
    document(snapshot, "snapshot", (1,2))
    obj(snapshot.get("sections"), "snapshot.sections")
    obj(snapshot.get("metadata", {}), "snapshot.metadata")
    for name in SECTIONS:
        if name not in snapshot["sections"]:
            continue
        section = snapshot["sections"][name]
        path = "snapshot.sections." + name
        obj(section, path)
        choice(section.get("status"), path+".status", ("ok", "error", "unavailable", "missing"))
        optional(section, "truncated", boolean, path)
        rows = section.get("rows", [])
        arr(rows, path+".rows")
        seen = set()
        for row in rows:
            obj(row, path+".row")
            string(row.get("relation"), path+".row.relation")
            if name in ("tables", "columns", "constraints", "indexes"):
                string(row.get("name"), path+".row.name")
            if name == "tables":
                string(row.get("schema"), path+".row.schema")
                choice(row.get("kind"), path+".row.kind", ("r", "p"))
                optional(row, "partition", boolean, path)
                optional(row, "has_inheritance_children", boolean, path)
            if name == "columns":
                string(row.get("type"), path+".row.type")
                if snapshot["schema_version"] == 1:
                    boolean(row.get("not_null"), path+".row.not_null")
                else:
                    boolean(row.get("column_not_null"), path+".row.column_not_null")
                    boolean(row.get("domain_not_null"), path+".row.domain_not_null")
                optional(row, "has_default", boolean, path)
                optional(row, "domain_has_check", boolean, path)
            if name == "constraints":
                choice(row.get("type"), path+".row.type", ("p", "u", "f", "c", "n"))
                strings(row.get("columns", []), path+".row.columns")
                optional(row, "validated", boolean, path)
                optional(row, "enforced", boolean, path)
                optional(row, "no_inherit", boolean, path)
                if row["type"] == "f":
                    string(row.get("referenced_relation"), path+".row.referenced_relation")
                    strings(row.get("referenced_columns"), path+".row.referenced_columns", True)
                    require(len(row.get("columns", [])) == len(row["referenced_columns"]),
                            path+".row", "foreign key column counts differ")
                    for key, allowed in (("match_type", ("s","f","p")),
                                         ("update_action", ("a","r","c","n","d")),
                                         ("delete_action", ("a","r","c","n","d"))):
                        if row.get(key) is not None:
                            choice(row[key], path+"."+key, allowed)
            if name == "indexes":
                arr(row.get("columns", []), path+".row.columns")
                for col in row.get("columns", []):
                    if col is not None:
                        string(col, path+".row.columns")
                for key in ("unique","valid","partial","expression"):
                    optional(row, key, boolean, path)
            if name == "statistics":
                string(row.get("column"), path+".row.column")
                if row.get("null_frac") is not None:
                    number(row["null_frac"], path+".row.null_frac")
                    require(row["null_frac"] <= 1, path+".row.null_frac", "must be <= 1")
            key = (row["relation"], row.get("name", row.get("column", "")))
            require(key not in seen, path, "duplicate row identity")
            seen.add(key)


def validate_contract(contract):
    document(contract, "contract")
    fields(contract, "contract", ("schema_version", "tables", "vocabulary"))
    arr(contract.get("tables", []), "contract.tables")
    seen = set()
    for table in contract.get("tables", []):
        p = "contract.tables[]"
        fields(table, p, ("relation","source","row_meaning","kind","required_columns","expected_name",
                          "column_names","relationships","column_groups","immutable_after","flags","exceptions"))
        string(table.get("relation"), p+".relation")
        require(table["relation"] not in seen, p, "duplicate relation")
        seen.add(table["relation"])
        for name in ("source","row_meaning","expected_name","immutable_after"):
            optional(table, name, string, p)
        if "kind" in table:
            choice(table["kind"], p+".kind", ("entity","event","projection","draft","cache"))
        optional(table, "required_columns", strings, p)
        if "column_names" in table:
            obj(table["column_names"], p+".column_names")
            for key, value in table["column_names"].items():
                string(key, p+".column_names.key")
                string(value, p+".column_names.value")
        for name in ("relationships","column_groups","flags","exceptions"):
            arr(table.get(name, []), p+"."+name)
        for rel in table.get("relationships", []):
            q = p+".relationships[]"
            fields(rel, q, ("columns","referenced_relation","referenced_columns","enforce","allow_null",
                            "delete_action","update_action","match_type","source"))
            strings(rel.get("columns"), q+".columns", True)
            if rel.get("enforce") is not False or "referenced_columns" in rel:
                strings(rel.get("referenced_columns"), q+".referenced_columns", True)
                require(len(rel["columns"]) == len(rel["referenced_columns"]), q, "column counts differ")
            if rel.get("enforce") is not False or "referenced_relation" in rel:
                string(rel.get("referenced_relation"), q+".referenced_relation")
            for name in ("enforce","allow_null"):
                optional(rel, name, boolean, q)
            optional(rel, "source", string, q)
            for name, allowed in (("delete_action",("a","r","c","n","d")),
                                  ("update_action",("a","r","c","n","d")),("match_type",("s","f","p"))):
                if name in rel:
                    choice(rel[name], q+"."+name, allowed)
        for group in table.get("column_groups", []):
            q = p+".column_groups[]"
            fields(group, q, ("name","columns","lifecycle","writers","created_by","finalized_by","retention"))
            string(group.get("name"), q+".name")
            strings(group.get("columns"), q+".columns", True)
            for name in ("lifecycle","created_by","finalized_by","retention"):
                optional(group, name, string, q)
            optional(group, "writers", strings, q)
        for flag in table.get("flags", []):
            q = p+".flags[]"
            fields(flag, q, ("columns","meaning","source_column","invariant","source"))
            strings(flag.get("columns"), q+".columns", True)
            choice(flag.get("meaning"), q+".meaning", ("boolean","state","event","derived","soft_delete"))
            for name in ("source_column","invariant","source"):
                optional(flag, name, string, q)
        for exception in table.get("exceptions", []):
            q = p+".exceptions[]"
            fields(exception, q, ("rule","reason","source"))
            choice(exception.get("rule"), q+".rule", RULES)
            string(exception.get("reason"), q+".reason")
            string(exception.get("source"), q+".source")
    if "vocabulary" in contract:
        fields(contract["vocabulary"], "contract.vocabulary", ("terms","allowed_abbreviations"))
        for name in ("terms","allowed_abbreviations"):
            optional(contract["vocabulary"], name, strings, "contract.vocabulary")


def validate_evidence(evidence):
    document(evidence, "evidence")
    fields(evidence, "evidence", ("schema_version","writes","profiles","contention"))
    for section in ("writes","profiles","contention"):
        arr(evidence.get(section, []), "evidence."+section)
        for item in evidence.get(section, []):
            p = "evidence."+section+"[]"
            allowed = {
                "writes": ("relation","operation","columns","business_operation","after_finalization","source","observed_at"),
                "profiles": ("relation","kind","columns","violations","rows_checked","scope","complete","source","observed_at",
                             "includes_children"),
                "contention": ("relation","operations","same_key","wait_ms","blocker_chain","transaction_boundary","source","observed_at"),
            }
            fields(item, p, allowed[section])
            string(item.get("relation"), p+".relation")
            optional(item, "source", string, p)
            optional(item, "observed_at", timestamp, p)
            if section == "writes":
                choice(item.get("operation"), p+".operation", ("INSERT","UPDATE","DELETE","TRUNCATE"))
                optional(item, "columns", strings, p)
                optional(item, "business_operation", string, p)
                optional(item, "after_finalization", boolean, p)
            elif section == "profiles":
                choice(item.get("kind"), p+".kind", ("null","flag_combination"))
                strings(item.get("columns"), p+".columns", True)
                for name in ("violations","rows_checked"):
                    require(type(item.get(name)) is int, p+"."+name, "expected integer")
                    number(item[name], p+"."+name)
                require(item["violations"] <= item["rows_checked"], p, "violations exceed checked rows")
                string(item.get("scope"), p+".scope")
                optional(item, "complete", boolean, p)
                optional(item, "includes_children", boolean, p)
            else:
                optional(item, "operations", strings, p)
                optional(item, "wait_ms", number, p)
                for name in ("same_key","blocker_chain","transaction_boundary"):
                    optional(item, name, boolean, p)


def load_module(name):
    spec = importlib.util.spec_from_file_location("schema_review_"+name, Path(__file__).with_name(name+".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def review(snapshot, contract=None, evidence=None):
    contract = {"schema_version":1,"tables":[]} if contract is None else contract
    evidence = {"schema_version":1} if evidence is None else evidence
    validate_snapshot(snapshot)
    validate_contract(contract)
    validate_evidence(evidence)
    coverage = {}
    for name in SECTIONS:
        part = snapshot["sections"].get(name, {})
        coverage[name] = {
            "status": part.get("status","missing"), "truncated": part.get("truncated",True),
            "rows": len(part.get("rows",[])),
        }
        if name == "statistics":
            coverage[name]["visibility"] = "role_visible_analyzed_columns_only"
            seen_stats = {(r["relation"],r["column"]) for r in part.get("rows",[])}
            coverage[name]["missing_columns"] = sum(
                (c["relation"],c["name"]) not in seen_stats
                for c in snapshot["sections"].get("columns",{}).get("rows",[]))
    table_section = snapshot["sections"].get("tables",{})
    tables = table_section.get("rows",[]) if table_section.get("status") == "ok" else []
    contracts = {t["relation"]:t for t in contract.get("tables",[])}
    relations = {t["relation"] for t in tables}
    findings = load_module("structure").review(snapshot,contract,evidence)
    findings += load_module("lifecycle").review(snapshot,contract,evidence)
    evidence_relations = {row["relation"] for name in ("writes","profiles","contention")
                          for row in evidence.get(name,[])}
    for relation in sorted((set(contracts) | evidence_relations) - relations):
        findings.append({"rule":"schema.coverage","relation":relation,"status":"needs_evidence",
                         "severity":"medium","confidence":"low",
                         "summary":"設計契約・観測情報の対象テーブルを収集結果で確認できません。",
                         "evidence":[],"recommendation":["対象schema・識別子・収集上限を確認する。"],
                         "verification":["対象テーブルを再収集する。"],"missing":["table inventory"]})
    for finding in findings:
        for name in ("evidence","recommendation","verification","missing"):
            finding.setdefault(name,[])
        exceptions = contracts.get(finding["relation"],{}).get("exceptions",[])
        exception = next((e for e in exceptions if e["rule"]==finding["rule"]),None)
        if exception:
            finding["underlying_status"] = finding["status"]
            finding["status"] = "exception"
            finding["exception"] = {"reason":exception["reason"],"source":exception["source"]}
        identity = "\x1f".join((finding["rule"],finding["relation"],finding["summary"],
                                  json.dumps(finding["evidence"],ensure_ascii=False,sort_keys=True)))
        finding["id"] = hashlib.sha256(identity.encode()).hexdigest()[:12]
    findings.sort(key=lambda f: ({"high":0,"medium":1,"low":2}[f["severity"]],f["relation"],f["rule"],f["id"]))
    groups = {}
    for finding in findings:
        group = finding.get("group",finding["relation"]+":"+finding["rule"])
        groups.setdefault(group,[]).append(finding["id"])
    limits = [
        "指摘ゼロは全体の健全性の保証ではありません。未入力の業務契約と未観測の処理は評価できません。",
        "catalogの観測時点とコード/trace/集計の時点・DB・対象範囲を照合してください。",
        "NULL統計は推定で、権限・RLS・ANALYZE・partitionの範囲により欠測します。",
        "正規化した証拠のsourceは参照位置です。自動で内容を取得・検証しません。",
        "observationsは入力レコード数です。静的な書込箇所も含み、実行観測件数ではありません。",
    ]
    if snapshot["schema_version"] == 1:
        limits.append("snapshot v1のnot_null=trueは列制約とdomainの由来・検証状態が不明です。"
                      "NULL禁止の保証を確定せず、v2 collectorで再収集してください。")
    for name, part in coverage.items():
        if part["status"] != "ok" or part["truncated"]:
            limits.append(name+": incomplete catalog evidence")
    for relation in sorted(relations-set(contracts)):
        limits.append(relation+": 業務上の設計契約が未入力です。")
    return {
        "schema_version":1, "snapshot_version":snapshot["schema_version"], "scope":{
            k:snapshot.get("metadata",{}).get(k) for k in
            ("database","server_version_num","collected_at","requested_schemas")
        },
        "coverage":coverage,
        "observations":{name:len(evidence.get(name,[])) for name in ("writes","profiles","contention")},
        "contract_tables":len(contracts),
        "summary":dict(Counter(f["status"] for f in findings)),
        "findings":findings, "groups":[{"key":key,"finding_ids":ids} for key,ids in sorted(groups.items())],
        "limitations":limits, "migration_checklist":MIGRATION,
    }


def md(value):
    text = html.escape(str(value), quote=False).replace("\n"," ").replace("\r"," ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text)


def markdown(report):
    lines = ["# PostgreSQL schema review", "", "## 対象と収集範囲", ""]
    for key,value in report["scope"].items():
        lines.append("- "+md(key)+": "+md(value))
    lines += ["", "| 項目 | 状態 | 打切り | 件数 |", "| --- | --- | --- | --- |"]
    for name,item in report["coverage"].items():
        lines.append("| "+ " | ".join(md(x) for x in
                     (name,item["status"],item["truncated"],item["rows"]))+" |")
    lines += ["", "## 制限・未確認", ""]
    lines += ["- "+md(x) for x in report["limitations"]]
    lines += ["", "## 改善候補", ""]
    if not report["findings"]:
        lines.append("入力された範囲で指摘はありません。上記の制限・未確認項目を確認してください。")
    for group in report["groups"]:
        lines += ["", "### "+md(group["key"]), ""]
        for finding in report["findings"]:
            if finding["id"] not in group["finding_ids"]:
                continue
            lines.append("**"+md(finding["rule"])+" — "+md(finding["summary"])+"**")
            lines += ["", "- 判定: "+md(finding["status"])+" / 重要度: "+md(finding["severity"])
                      +" / 確度: "+md(finding["confidence"])]
            if "exception" in finding:
                lines.append("- 例外: "+md(finding["exception"]["reason"])+" / 根拠: "
                             +md(finding["exception"]["source"])+" / 元の判定: "+md(finding["underlying_status"]))
            for key,label in (("evidence","根拠"),("missing","追加調査"),
                               ("recommendation","改善候補"),("verification","検証")):
                for value in finding[key]:
                    lines.append("- "+label+": "+md(value))
            lines.append("")
    lines += ["", "## 移行時の確認", ""]
    lines += ["- "+md(x) for x in report["migration_checklist"]]
    return "\n".join(lines)+"\n"


def read_json(path, label):
    if path.stat().st_size > 32*1024*1024:
        raise InputError(label+": file exceeds 32 MiB")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"),
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):
        raise InputError(label+": invalid JSON") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot",type=Path)
    parser.add_argument("--contract",type=Path)
    parser.add_argument("--evidence",type=Path)
    parser.add_argument("--format",choices=("json","markdown"),default="markdown")
    parser.add_argument("--output",type=Path)
    args = parser.parse_args(argv)
    try:
        report = review(read_json(args.snapshot,"snapshot"),
                        read_json(args.contract,"contract") if args.contract else None,
                        read_json(args.evidence,"evidence") if args.evidence else None)
    except (InputError,OSError) as exc:
        # No raw JSON values, connection info or SQL in CLI errors.
        print("schema review input error: "+(str(exc) if isinstance(exc,InputError) else "file unavailable"),
              file=sys.stderr)
        return 2
    rendered = json.dumps(report,ensure_ascii=False,indent=2)+"\n" if args.format=="json" else markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(rendered,encoding="utf-8")
    else:
        print(rendered,end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
