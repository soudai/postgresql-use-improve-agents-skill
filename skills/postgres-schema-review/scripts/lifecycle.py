"""Evidence-scoped lifecycle, responsibility, immutability and contention review.

This module never executes SQL. Inputs are normalized catalog metadata, declared
business semantics and observations; static SQL alone is not runtime evidence.
"""
from __future__ import annotations

from itertools import combinations
from math import isfinite


def _rows(snapshot: dict, section: str) -> list[dict]:
    value = snapshot.get("sections", {}).get(section, {})
    if value.get("status") != "ok":
        return []
    return value.get("rows", [])


def _source(value: dict) -> bool:
    return isinstance(value.get("source"), str) and bool(value["source"].strip())


def _observed(value: dict) -> bool:
    return _source(value) and isinstance(value.get("observed_at"), str) and bool(value["observed_at"].strip())


def _point(value: dict) -> str:
    source = value.get("source") or "出典未確認"
    time = value.get("observed_at")
    return str(source) + (" @ " + str(time) if time else "")


def _finding(rule: str, relation: str, status: str, summary: str,
             facts: list[str], recommendation: list[str], verification: list[str],
             missing: list[str] | None = None, severity: str = "medium",
             confidence: str = "medium") -> dict:
    return {
        "rule": "schema." + rule, "relation": relation, "status": status,
        "severity": severity, "confidence": confidence, "summary": summary,
        "evidence": facts, "recommendation": recommendation,
        "verification": verification, "missing": list(dict.fromkeys(missing or [])),
        "group": relation + ":lifecycle",
    }


def _catalog_missing(snapshot: dict, relation: str, groups: list[dict]) -> list[str]:
    missing = []
    if relation not in {r.get("relation") for r in _rows(snapshot, "tables")}:
        missing.append("対象テーブルを同じDB・調査範囲のカタログで確認する")
    columns = {c.get("name") for c in _rows(snapshot, "columns")
               if c.get("relation") == relation}
    declared = {c for g in groups for c in g.get("columns", [])}
    absent = sorted(declared - columns)
    if absent:
        missing.append("設計意図にある列の存在・名前・収集範囲を確認する: " + ", ".join(absent))
    return missing


def _independent_columns(left: dict, right: dict) -> bool:
    a, b = set(left.get("columns", [])), set(right.get("columns", []))
    return bool(a - b and b - a)


def _group_pairs(groups: list[dict]) -> list[tuple[dict, dict]]:
    return [(a, b) for a, b in combinations(groups, 2)
            if _independent_columns(a, b)]


def _lifecycle_difference(a: dict, b: dict) -> list[str]:
    return [field for field in ("lifecycle", "created_by", "finalized_by", "retention")
            if a.get(field) and b.get(field) and a[field] != b[field]]


def _group_fact(group: dict) -> str:
    details = []
    for key in ("lifecycle", "created_by", "finalized_by", "retention"):
        if group.get(key):
            details.append(key + "=" + str(group[key]))
    if group.get("writers"):
        details.append("writers=" + ", ".join(group["writers"]))
    return (str(group.get("name", "unnamed")) + " [" +
            ", ".join(group.get("columns", [])) + "]: " + "; ".join(details))


def _write_matches(write: dict, groups: list[dict]) -> set[int]:
    cols = set(write.get("columns", []))
    return {index for index, group in enumerate(groups)
            if cols & set(group.get("columns", []))}


def _independent_writes(writes: list[dict], groups: list[dict],
                        operations: set[str]) -> bool:
    eligible = [w for w in writes if w.get("business_operation") in operations
                and w.get("operation") == "UPDATE" and _observed(w)]
    for a, b in combinations(eligible, 2):
        if a.get("business_operation") == b.get("business_operation"):
            continue
        if not _independent_columns(a, b):
            continue
        for ai in _write_matches(a, groups):
            for bi in _write_matches(b, groups):
                exclusive_a = set(a.get("columns", [])) - set(b.get("columns", []))
                exclusive_b = set(b.get("columns", [])) - set(a.get("columns", []))
                if (ai != bi and _independent_columns(groups[ai], groups[bi])
                        and exclusive_a & set(groups[ai].get("columns", []))
                        and exclusive_b & set(groups[bi].get("columns", []))):
                    # Independent group lifecycles or writers supply the domain
                    # distinction; arbitrary column grouping alone cannot.
                    aw, bw = set(groups[ai].get("writers", [])), set(groups[bi].get("writers", []))
                    if _lifecycle_difference(groups[ai], groups[bi]) or (aw and bw and aw != bw):
                        return True
    return False


def _review_groups(snapshot: dict, table: dict, writes: list[dict]) -> list[dict]:
    relation = table["relation"]
    groups = table.get("column_groups", [])
    if len(groups) < 2:
        return []
    # An explicitly sourced read model/cache intentionally combines facts.
    # It can still be diagnosed independently for actual write contention.
    if table.get("kind") in ("projection", "cache") and _source(table) and table.get("row_meaning"):
        return []
    pairs = _group_pairs(groups)
    base_missing = _catalog_missing(snapshot, relation, groups)
    if not _source(table):
        base_missing.append("ライフサイクル・責務を定めた設計資料の出典")
    facts = [_group_fact(g) for g in groups]
    if _source(table):
        facts.append("設計意図: " + _point(table))
    result = []
    lifecycle_pairs = [(a, b) for a, b in pairs if _lifecycle_difference(a, b)]
    if lifecycle_pairs:
        missing = list(base_missing)
        result.append(_finding(
            "lifecycle", relation, "needs_evidence" if missing else "candidate",
            "独立した発生・確定・保持条件を持つ列群が同じ行にある",
            facts,
            ["各列群の生成・確定・訂正・削除の契機を確認し、独立した事実の分離を比較する",
             "分割後も同時に保証すべき業務上の整合性と、現在状態を表す参照用モデルを定める"],
            ["再試行・複数回発生・保存期限の違いを含め、分割前後の業務結果と履歴を比較する",
             "JOIN増加、権限・RLS、移行中の整合性と復旧方法を検証する"],
            missing, confidence="low" if missing else "medium",
        ))
    responsibility_pairs = [(a, b) for a, b in pairs
                            if a.get("writers") and b.get("writers")
                            and set(a["writers"]) != set(b["writers"])]
    if responsibility_pairs:
        missing = list(base_missing)
        observed_ops = sorted({w.get("business_operation") for w in writes
                               if w.get("business_operation") and _observed(w)})
        responsibility_facts = list(facts)
        if observed_ops:
            responsibility_facts.append("観測した更新業務: " + ", ".join(observed_ops))
        result.append(_finding(
            "responsibility", relation, "needs_evidence" if missing else "candidate",
            "異なる業務が独立した列群の更新を担当している",
            responsibility_facts,
            ["各行が表す一つの事実と、更新責務・権限・保存期間の境界を確認する",
             "責務ごとの分割と一体で更新する必要性を比較し、列数だけで分割を決めない"],
            ["業務ごとの実際の変更列を追跡し、ORMによる全列UPDATEと区別する",
             "分割前後で許可される状態遷移、多重度、結果と同時実行時の整合性を比較する"],
            missing, confidence="low" if missing else "medium",
        ))
    return result


def _review_immutable(snapshot: dict, table: dict, writes: list[dict]) -> list[dict]:
    relation = table["relation"]
    mutable_ops = [w for w in writes if w.get("operation") in ("UPDATE", "DELETE", "TRUNCATE")]
    boundary = table.get("immutable_after")
    # "event" is a useful question, not an automatic prohibition on corrections.
    if not boundary:
        if table.get("kind") != "event" or not mutable_ops:
            return []
        return [_finding(
            "immutable", relation, "needs_evidence",
            "イベントの更新・削除について、確定時点と訂正方針の確認が必要",
            ["書込み: " + w.get("operation", "") + " / " + _point(w) for w in mutable_ops],
            ["事実の確定時点と訂正・取消・保存期限の方針を定義する",
             "確定した履歴を残す必要がある場合、訂正イベントと現在状態の分離を検討する"],
            ["元の事実、訂正理由、訂正後の結果を追跡できるか確認する"],
            ["不変性を要求する設計意図とその出典"], confidence="low",
        )]
    if not mutable_ops:
        # INSERT-only observations do not establish the absence of later changes.
        return [_finding(
            "immutable", relation, "needs_evidence",
            "確定後の不変性を検証する更新・削除の観測が不足している",
            ["不変にする時点: " + str(boundary)] + (["設計意図: " + _point(table)] if _source(table) else []),
            ["確定済みの事実に対する書込み経路、トリガー、訂正・削除方針を調べる"],
            ["代表的な業務期間と保持期限の処理を対象に、確定後の変更があるか確認する"],
            ["確定前後を区別できる書込み観測と観測範囲"], confidence="low",
        )]
    relevant = [w for w in mutable_ops if w.get("after_finalization") is not False or not _observed(w)]
    if not relevant:
        # All supplied operations were explicitly scoped before finalization.
        # The caller still reports observation coverage; this is not global health.
        return []
    catalog_missing = _catalog_missing(snapshot, relation, [])
    source_missing = [] if _source(table) else ["不変性を定めた設計資料の出典"]
    confirmed = [w for w in relevant if w.get("after_finalization") is True and _observed(w)]
    missing = catalog_missing + source_missing
    if not confirmed:
        if any(w.get("after_finalization") is not True for w in relevant):
            missing.append("UPDATE/DELETE/TRUNCATEの対象が確定済みだったことを示す観測")
        if any(not _observed(w) for w in relevant):
            missing.append("実行された書込みの出典と観測時刻")
    status = "confirmed" if confirmed and not missing else "needs_evidence"
    facts = ["不変にする時点: " + str(boundary)]
    if _source(table):
        facts.append("設計意図: " + _point(table))
    for w in confirmed if status == "confirmed" else relevant:
        facts.append(w.get("operation", "") + " / after_finalization=" +
                     str(w.get("after_finalization")) + " / " + _point(w))
    return [_finding(
        "immutable", relation, status,
        "不変にすべき確定済みの行へのUPDATE/DELETE/TRUNCATEが観測された" if status == "confirmed"
        else "確定済みの事実に対する更新・削除か追加確認が必要",
        facts,
        ["許可された訂正・取消・保存期限による削除かを設計意図と照合する",
         "履歴を上書きする経路は、訂正・取消イベントの追加と現在状態の再構築に置き換える案を比較する"],
        ["確定前の編集、確定後の訂正、期限到来時の削除をそれぞれ検証する",
         "履歴・監査・集計の意味を保ち、UPDATE対象列と実際の値変更を区別する"],
        missing, severity="high" if status == "confirmed" else "medium",
        confidence="high" if status == "confirmed" else "low",
    )]


def _review_contention(snapshot: dict, table: dict, writes: list[dict],
                       observations: list[dict]) -> list[dict]:
    relation = table["relation"]
    groups = table.get("column_groups", [])
    result = []
    for observation in observations:
        wait = observation.get("wait_ms")
        numeric = isinstance(wait, (int, float)) and not isinstance(wait, bool) and isfinite(wait)
        operations = set(observation.get("operations", []))
        if numeric and wait <= 0:
            continue
        if observation.get("same_key") is False:
            # A known different-key wait does not prove the targeted row hotspot.
            continue
        if len(operations) == 1:
            # A single operation's necessary serialization is not a god table.
            continue
        missing = _catalog_missing(snapshot, relation, groups)
        if not numeric:
            missing.append("実測された待機時間")
        if len(operations) < 2:
            missing.append("同一行を更新する複数の業務操作")
        if observation.get("same_key") is not True:
            missing.append("競合する書込みが同一行キーだったことの証拠")
        if observation.get("blocker_chain") is not True:
            missing.append("待機元から根本blockerまでの待機チェーン")
        if observation.get("transaction_boundary") is not True:
            missing.append("ロック取得から終了までのトランザクションと書込みの対応")
        if not _observed(observation):
            missing.append("競合観測の出典と観測時刻")
        if not _source(table):
            missing.append("業務ごとの責務・ライフサイクルを示す設計資料の出典")
        independent = _independent_writes(writes, groups, operations)
        if not independent:
            missing.append("複数業務による独立した列群への書込みと、その出典・時刻")
        status = "confirmed" if not missing else "needs_evidence"
        facts = ["業務操作: " + ", ".join(sorted(operations)),
                 "待機時間(ms): " + (str(wait) if numeric else "未確認"),
                 "same_key=" + str(observation.get("same_key")),
                 "blocker_chain=" + str(observation.get("blocker_chain")),
                 "transaction_boundary=" + str(observation.get("transaction_boundary")),
                 "観測: " + _point(observation)]
        if status == "confirmed":
            facts += ["書込み: " + str(w.get("business_operation")) + " [" +
                      ", ".join(w.get("columns", [])) + "] / " + _point(w)
                      for w in writes if w.get("business_operation") in operations and _observed(w)]
        result.append(_finding(
            "contention", relation, status,
            "異なる責務の更新が同一行で競合している" if status == "confirmed"
            else "更新集中とテーブルの責務を結び付ける証拠が不足している",
            facts,
            ["独立した列群の分離と、トランザクション短縮・更新順序の見直しを比較する",
             "在庫・採番など必要な直列化とDDL待ちを切り分け、分割で改善する範囲を示す"],
            ["同じ業務負荷で待機時間、p95/p99、失敗・再試行、データの整合性を比較する",
             "pg_stat_statementsだけで同一キーや処理順を推定しない",
             "RowExclusiveLockの保持件数を競合件数にせず、transaction ID待ちと実際の書込みを照合する"],
            missing, severity="high" if status == "confirmed" else "medium",
            confidence="high" if status == "confirmed" else "low",
        ))
    return result


def review(snapshot: dict, contract: dict, evidence: dict) -> list[dict]:
    """Return actionable candidates and evidence gaps, never a global health claim."""
    result = []
    tables = {t["relation"]: t for t in contract.get("tables", [])}
    # Observations for an undeclared table remain reviewable, but cannot establish
    # a violation of unknown business semantics.
    for entry in evidence.get("contention", []):
        relation = entry.get("relation")
        if relation and relation not in tables:
            tables[relation] = {"relation": relation}
    for relation, table in tables.items():
        writes = [w for w in evidence.get("writes", []) if w.get("relation") == relation]
        observations = [o for o in evidence.get("contention", []) if o.get("relation") == relation]
        result.extend(_review_groups(snapshot, table, writes))
        result.extend(_review_immutable(snapshot, table, writes))
        result.extend(_review_contention(snapshot, table, writes, observations))
    return result
