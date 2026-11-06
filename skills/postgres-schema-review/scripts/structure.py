"""Evidence-based schema structure diagnostics; no database access or DDL."""
from __future__ import annotations

from difflib import get_close_matches
import re


def _rows(snapshot, name):
    section = snapshot.get("sections", {}).get(name, {})
    return section.get("rows", []) if section.get("status") == "ok" else []


def _complete(snapshot, name):
    section = snapshot.get("sections", {}).get(name, {})
    return section.get("status") == "ok" and section.get("truncated") is False


def _finding(rule, relation, status, summary, facts=(), missing=(), severity="medium",
             recommendation=(), verification=(), group=None):
    result = {
        "rule": "schema." + rule, "relation": relation, "status": status,
        "severity": severity, "confidence": "high" if status == "confirmed" else
        ("medium" if status in ("candidate", "exception") else "low"),
        "summary": summary, "evidence": list(facts), "missing": list(missing),
        "recommendation": list(recommendation), "verification": list(verification),
    }
    if group:
        result["group"] = group
    return result


def _source(table, item=None):
    return (item or {}).get("source") or table.get("source")


def _verdict(source, observed=True):
    return "confirmed" if source and observed else "needs_evidence"


def _provenance(item):
    return bool(item.get("source") and item.get("observed_at"))


def _pairs(item):
    local, remote = item.get("columns", []), item.get("referenced_columns", [])
    if not local or len(local) != len(remote) or len(set(local)) != len(local):
        return None
    # A permutation on both sides is equivalent; swapping just one side is not.
    return sorted(zip(local, remote))


def _column_null_status(name, columns, constraints):
    column = columns.get(name, {})
    value = column.get("column_not_null")
    if value is False:
        return "absent"
    if value is not True:
        return "unknown"
    # attnotnull is set for NOT VALID too. A domain NOT NULL is not a
    # column guarantee: a typed NULL can bypass the domain conversion check.
    proofs = [c for c in constraints if c.get("type") in ("n","p")
              and name in c.get("columns", [])]
    if any(c.get("validated") is True and c.get("enforced") is True for c in proofs):
        return "validated"
    if any(c.get("enforced") is False for c in proofs):
        return "unenforced"
    if any(c.get("validated") is False for c in proofs):
        return "unvalidated"
    return "unknown"


def _null_blocked_by_match_full(name, columns, constraints):
    # MATCH FULL rejects partial NULL. Only a validated column-level guarantee
    # excludes all-NULL existing rows; domains and NOT VALID do not suffice.
    return any(
        c.get("type") == "f" and c.get("match_type") == "f" and
        c.get("validated") is True and c.get("enforced") is True and
        name in c.get("columns", []) and
        any(_column_null_status(member, columns, constraints) == "validated"
            for member in c.get("columns", []))
        for c in constraints
    )


def _required_null_findings(snapshot, table, columns, constraints, names, source,
                            rule="null", context=(), inheritance_uncertain=False):
    findings = []
    for name in names:
        column = columns.get(name)
        state = _column_null_status(name, columns, constraints)
        if state == "validated" or _null_blocked_by_match_full(name, columns, constraints):
            if not inheritance_uncertain:
                continue
            state = "scope_unknown"
        facts = list(context) + ["required column: " + name]
        if source:
            facts.append("contract: " + source)
        facts.append("column NOT NULL guarantee: " + state)
        if column and column.get("domain_not_null") is True:
            facts.append("domain_not_null=true; domainの型変換時の検査は列のNULL禁止保証と異なる")
        missing = [] if source else ["必須性を定めた業務上の契約の出典"]
        observed = state not in ("unknown", "scope_unknown")
        summary = "必須列のNULL禁止保証を確認できません。"
        if state == "unvalidated":
            summary = "必須列のNOT NULLが既存データに対して未検証です。"
            facts.append("NOT NULL: 既存行は未検証 (validated=false)")
        elif state == "unenforced":
            summary = "必須列のNOT NULLが強制されていません。"
            facts.append("NOT NULL: enforced=false")
        elif state == "scope_unknown":
            missing.append("通常継承子の有無・各子の列保証と、契約が親ONLYか継承子を含むか")
            facts.append("親の局所制約だけでは、通常継承子を含むNULL禁止を保証しない")
            if any(c.get("no_inherit") is True and name in c.get("columns", []) for c in constraints):
                facts.append("NO INHERITの制約は子へ伝播しない")
        elif state == "unknown":
            missing.append("列NOT NULLの由来・validated/enforced状態。v1はv2で再収集する")
        elif state == "absent":
            facts.append("column_not_null=false")
        status = _verdict(source, observed)
        if state == "absent" and (column.get("domain_has_check") is True or
                not _complete(snapshot,"constraints") or any(c.get("type") == "c" for c in constraints)):
            status = "candidate" if source else "needs_evidence"
            missing.append("CHECK等が必須性を保証しているか（式は通常収集の対象外）")
        findings.append(_finding(rule, table["relation"], status, summary, facts, missing,
            recommendation=("domainと列制約を分け、既存NULLの修復・列NOT NULLの追加/検証を計画する。",
                            "新規書込みへの強制と、既存行の検証をそれぞれ確認する。"),
            verification=("空のスカラー副問い合わせ由来のNULL、新規INSERT/UPDATE、既存行を検証する。",
                          "MATCH FULLの全列NULLを禁止するには検証済みの列NOT NULL等の根拠が必要。"),
            group=table["relation"] + ":lifecycle"))
    return findings


def _fk_findings(snapshot, table, columns, constraints, inheritance_uncertain=False):
    relation = table["relation"]
    findings = []
    for expected in table.get("relationships", []):
        source = _source(table, expected)
        names = expected.get("columns", [])
        target = expected.get("referenced_relation")
        facts = [f"declared reference: {names} -> {target} {expected.get('referenced_columns', [])}"]
        if source:
            facts.append("contract: " + source)
        if expected.get("allow_null") is False:
            findings.extend(_required_null_findings(
                snapshot, table, columns, constraints, names, source,
                rule="null" if expected.get("enforce") is False else "fk", context=facts,
                inheritance_uncertain=inheritance_uncertain))
        if expected.get("enforce") is False:
            findings.append(_finding("fk", relation, "exception" if source else "needs_evidence",
                "参照整合性をFKで保証しない設計として申告されています。",
                facts, () if source else ("例外の根拠・整合性を保証する場所",), severity="low",
                recommendation=("外部ID・非同期連携等の目的と孤児を扱う方針を記録する。",),
                verification=("例外の適用範囲と参照元・参照先の削除運用を確認する。",)))
            continue
        if expected.get("enforce") is not True or _pairs(expected) is None:
            findings.append(_finding("fk", relation, "needs_evidence",
                "参照先とFKで保証する範囲を確認してください。", facts,
                ("enforceと対応する複合キー列の宣言",)))
            continue
        matches = [c for c in constraints if c.get("type") == "f" and
                   c.get("referenced_relation") == target and _pairs(c) == _pairs(expected)]
        if not matches:
            complete = _complete(snapshot, "constraints")
            missing = []
            if not complete:
                missing.append("制約一覧が未収集または打切り")
            if not source:
                missing.append("FKを必要とする設計意図の出典")
            findings.append(_finding("fk", relation, _verdict(source, complete),
                "宣言された列対応を保証するFKが見つかりません。" if complete else
                "宣言された参照を保証するFKの有無を確認できません。",
                facts, missing, severity="high",
                recommendation=("参照先の候補キー・型・tenant境界・既存の孤児を確認し、対象FKの追加を検討する。",),
                verification=("列の対応順と多重度を確認し、存在しない参照先とtenantを跨ぐ参照が拒否されるか検証する。",
                              "対象版のロック・NOT VALIDからVALIDATEまでの適用手順を確認する。")))
        for actual in matches:
            issues, unknown = [], []
            if actual.get("enforced") is False:
                issues.append("FKがenforcedではない")
            elif actual.get("enforced") is not True:
                unknown.append("FKのenforced状態")
            if actual.get("validated") is False:
                issues.append("既存データに対して未検証のFK")
            elif actual.get("validated") is not True:
                unknown.append("FKのvalidated状態")
            for attribute in ("delete_action", "update_action", "match_type"):
                if attribute in expected:
                    if actual.get(attribute) is None:
                        unknown.append(attribute)
                    elif actual[attribute] != expected[attribute]:
                        issues.append(f"{attribute}: observed={actual[attribute]}, expected={expected[attribute]}")
            if issues or unknown:
                if not source:
                    unknown.append("期待する制約の設計意図の出典")
                findings.append(_finding("fk", relation, _verdict(source, bool(issues)),
                    "既存FKの保証範囲と設計意図を照合してください。",
                    facts + ["constraint: " + actual.get("name", "(unnamed)")] + issues,
                    unknown, severity="high" if actual.get("enforced") is False else "medium",
                    recommendation=("既存FKの検証状態・MATCH・更新/削除動作を確認し、必要な差分だけ計画する。",),
                    verification=("既存データと新規書込みを分けて確認し、期待する削除・更新・NULLの動作を検証する。",)))
    return findings


def _name_findings(snapshot, table, actual, columns, vocabulary):
    relation, findings = table["relation"], []
    source = table.get("source")
    expected = table.get("expected_name")
    if expected and actual and expected != actual.get("name"):
        findings.append(_finding("naming", relation, _verdict(source),
            "テーブル名が宣言された業務用語と一致しません。",
            [f"observed: {actual.get('name')}; expected: {expected}"] +
            (["contract: " + source] if source else []),
            () if source else ("用語集・命名規約の出典",), severity="low",
            recommendation=("SQL・ORM・BI・ETL・APIの依存先を確認して改名案を作る。",),
            verification=("互換期間と旧名称への参照がなくなる条件を決める。",)))
    for old, new in table.get("column_names", {}).items():
        if old == new or old not in columns:
            continue
        findings.append(_finding("naming", relation, _verdict(source),
            "列名が宣言された業務用語と一致しません。",
            [f"column: {old}; expected: {new}"] + (["contract: " + source] if source else []),
            () if source else ("用語集・命名規約の出典",), severity="low",
            recommendation=("改名先の衝突と利用箇所を調べ、互換性を保つ移行を計画する。",),
            verification=("アプリ・分析・バッチの参照先を検証する。",)))
    terms = {str(term).lower() for term in vocabulary.get("terms", [])}
    allowed = terms | {str(a).lower() for a in vocabulary.get("allowed_abbreviations", [])}
    identifiers = ([actual["name"]] if actual else []) + sorted(columns)
    declared = set(table.get("column_names", {}))
    if expected and actual:
        declared.add(actual.get("name"))
    for identifier in identifiers:
        if identifier in declared:
            continue
        for token in sorted(set(re.findall(r"[a-zA-Z]+", identifier.lower()))):
            if len(token) < 4 or token in allowed:
                continue
            close = get_close_matches(token, sorted(terms), n=2, cutoff=0.86)
            if len(close) != 1:
                continue
            findings.append(_finding("naming", relation, "candidate",
                "用語集と似た綴りの識別子があります。",
                [f"identifier: {identifier}; token: {token}; glossary candidate: {close[0]}"],
                ("業務用語・許容略語として正当かの確認",), severity="low",
                recommendation=("用語の意味と利用箇所を確認してから改名候補にする。",),
                verification=("固有語と意図した単複を誤字扱いしていないか確認する。",)))
    return findings


def _profile_findings(table, evidence, columns, constraints, inheritance_uncertain=False):
    relation, findings = table["relation"], []
    required = set(table.get("required_columns", []))
    for expected in table.get("relationships", []):
        if expected.get("allow_null") is False:
            required.update(expected.get("columns", []))
    for profile in evidence.get("profiles", []):
        if profile.get("relation") != relation or profile.get("violations", 0) <= 0:
            continue
        names = set(profile.get("columns", []))
        kind = profile.get("kind")
        if kind not in ("null", "flag_combination"):
            continue
        missing = []
        if not _provenance(profile):
            missing.append("集計の出典と観測日時")
        if not profile.get("scope"):
            missing.append("集計した業務状態と対象範囲")
        if not names or not names.issubset(columns):
            missing.append("集計対象列のスキーマ情報")
        if kind == "null":
            source = table.get("source")
            if not source and names:
                related = [r for r in table.get("relationships", [])
                           if r.get("allow_null") is False and _source(table, r)]
                covered = {name for r in related for name in r.get("columns", [])}
                if names.issubset(covered):
                    source = "; ".join(sorted({_source(table, r) for r in related
                                               if names & set(r.get("columns", []))}))
            if not names or not names.issubset(required):
                missing.append("対象列を必須とする業務上の契約")
            # A multi-column violation can be NULL in any required member.
            # One protected member does not contradict NULL in another member.
            if names and all(_column_null_status(name, columns, constraints) == "validated" or
                             _null_blocked_by_match_full(name, columns, constraints) for name in names):
                if not inheritance_uncertain:
                    missing.append("現在のNULL禁止保証とNULL違反集計の時点・対象の整合")
                elif profile.get("includes_children") is False:
                    missing.append("ONLYで集計したNULL違反と親の局所NULL禁止保証の時点・対象の整合")
                elif profile.get("includes_children") is not True:
                    missing.append("集計が親ONLYか通常継承子を含むか（includes_children）の確認")
            rule, summary = "null", "必須性に反するNULLの集計が報告されています。"
            recommendation = "NULLの意味を確認し、補完・ライフサイクル分割・必須制約の適用を比較する。"
        else:
            matching = [f for f in table.get("flags", []) if names &
                        set(f.get("columns", [])) and names.issubset(
                            set(f.get("columns", [])) | {f.get("source_column")})]
            explicit = [f for f in matching if f.get("invariant")]
            source = _source(table, explicit[0] if explicit else None)
            if not explicit:
                missing.append("許されるフラグの組合せ・状態遷移を定めたinvariant")
            invariant_columns = {name for flag in explicit for name in flag.get("columns", [])}
            invariant_columns.update(flag["source_column"] for flag in explicit if flag.get("source_column"))
            if not invariant_columns.issubset(columns):
                missing.append("invariantが参照する全フラグ列・正本列のスキーマ情報: " +
                               ", ".join(sorted(invariant_columns - set(columns))))
            rule, summary = "flag", "許可されないフラグの組合せの集計が報告されています。"
            recommendation = "状態・イベント日時・導出値の正本を決め、不正な組合せを防ぐ構造を比較する。"
        if not source:
            missing.append("業務上の契約の出典")
        facts = [f"columns: {sorted(names)}; violations={profile.get('violations')}; "
                 f"rows_checked={profile.get('rows_checked')}; complete={profile.get('complete')}",
                 "scope: " + str(profile.get("scope", "(unknown)"))]
        if "includes_children" in profile:
            facts.append("includes_children: " + str(profile["includes_children"]))
        if source:
            facts.append("contract: " + source)
        if profile.get("source"):
            facts.append("profile: " + profile["source"])
        if profile.get("observed_at"):
            facts.append("observed_at: " + profile["observed_at"])
        findings.append(_finding(rule, relation, "confirmed" if not missing else "needs_evidence",
            summary, facts, missing, severity="high",
            recommendation=(recommendation,),
            verification=("集計対象の状態・時刻・権限による可視範囲と修正後の違反件数を検証する。",),
            group=relation + ":lifecycle"))
    return findings


def _null_findings(snapshot, table, columns, inheritance_uncertain=False):
    constraints = [c for c in _rows(snapshot,"constraints") if c.get("relation") == table["relation"]]
    return _required_null_findings(snapshot, table, columns, constraints,
                                   table.get("required_columns", []), table.get("source"),
                                   inheritance_uncertain=inheritance_uncertain)


def _flag_findings(table, columns):
    relation, findings = table["relation"], []
    for flag in table.get("flags", []):
        names = flag.get("columns", [])
        meaning = flag.get("meaning")
        if meaning == "boolean":
            continue
        source = _source(table, flag)
        facts = [f"columns: {names}; meaning: {meaning}"]
        if source:
            facts.append("contract: " + source)
        missing = []
        if not names or any(name not in columns for name in names):
            missing.append("フラグ列の定義")
        if not source:
            missing.append("状態・イベント・導出値の意味を定めた出典")
        if meaning in ("event", "derived") and flag.get("source_column"):
            facts.append("source column: " + flag["source_column"])
            if flag["source_column"] not in columns:
                missing.append("正本となる列の定義")
        elif meaning not in ("state", "soft_delete"):
            missing.append("フラグの意味と正本")
        findings.append(_finding("flag", relation, "needs_evidence" if missing else "candidate",
            "フラグが表す状態・事実と正本を確認してください。",
            facts, missing,
            recommendation=("独立した真偽属性、状態、イベント、導出値、論理削除を区別し、許される組合せと遷移を明記する。",),
            verification=("フラグと日時の矛盾、確定済み事実の反転、更新経路の不一致を確認する。",),
            group=relation + ":lifecycle"))
    return findings


def review(snapshot: dict, contract: dict, evidence: dict) -> list[dict]:
    """Return findings from observed schema and declared semantics.

    Missing observations never establish absence. Catalog completeness permits
    absence claims only inside the collector's scope. Profiles are counts of
    violations of a separately declared invariant, never raw NULL/false counts.
    """
    observed_tables = {r["relation"]: r for r in _rows(snapshot, "tables")}
    table_contracts = {r["relation"]: r for r in contract.get("tables", [])}
    all_columns = _rows(snapshot, "columns")
    all_constraints = _rows(snapshot, "constraints")
    findings = []
    for relation in sorted(set(observed_tables) | set(table_contracts)):
        table = table_contracts.get(relation, {"relation": relation})
        actual = observed_tables.get(relation)
        if actual is None:
            # The report layer emits schema.coverage for unmatched contracts.
            continue
        columns = {r["name"]: dict(r) for r in all_columns if r.get("relation") == relation}
        if snapshot.get("schema_version") != 2:
            # v1 conflated column and domain NOT NULL and omitted validation.
            # false establishes absence; true must never become a guarantee.
            for column in columns.values():
                column["column_not_null"] = False if column.get("not_null") is False else None
                column["domain_not_null"] = None
        constraints = [r for r in all_constraints if r.get("relation") == relation]
        # Parent-local NOT NULL/PK/FK metadata does not establish descendant
        # guarantees for ordinary inheritance. Missing scope metadata stays unknown.
        inheritance_uncertain = actual.get("has_inheritance_children") is not False
        findings.extend(_fk_findings(snapshot, table, columns, constraints, inheritance_uncertain))
        findings.extend(_name_findings(snapshot, table, actual, columns, contract.get("vocabulary", {})))
        findings.extend(_null_findings(snapshot, table, columns, inheritance_uncertain))
        findings.extend(_flag_findings(table, columns))
        findings.extend(_profile_findings(table, evidence, columns, constraints, inheritance_uncertain))
    return findings
