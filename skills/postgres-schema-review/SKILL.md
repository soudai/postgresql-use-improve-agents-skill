---
name: postgres-schema-review
description: Review PostgreSQL table design for missing constraints, naming inconsistencies, mixed lifecycles and responsibilities, mutable facts, nullable or flag semantics, and contention. Use to report refactoring candidates from schema snapshots, domain contracts and observed writes.
---

# PostgreSQLのテーブル設計レビュー

対象DBとschema、1行が表す事実、業務上の関連・必須性を確認する。取得済みのスキーマ、migration、ORM、業務資料から先に調べる。DDLだけで確定できない責務・ライフサイクルは、出典付きの設計契約と書込処理で補う。

1. [入力と収集](references/evidence.md)に従い、catalog snapshot、設計契約、観測情報を用意する。実DBの収集は指定されたschemaのcatalogを読み取り、データ値やSQL本文を取得しない。
2. `scripts/review.py`で候補を抽出し、[判定基準](references/rules.md)を使って根拠と反例を確認する。未取得・打切り・業務仕様不明を報告し、観測範囲を越えて問題なしと判定しない。
3. MarkdownまたはJSONで対象、重要度、確度、根拠、改善候補、追加調査、検証方法を報告する。同じライフサイクルの混在から生じたNULL・flag・多責務は、一つの改善テーマにまとめる。
4. [変更計画と検証](references/refactoring.md)に沿い、分割後の多重度・整合性、既存アプリとの共存、権限/RLS、復旧条件を示す。変更の実行やIssue投稿は、その依頼で許可された範囲で行う。

`*_id`、列数、NULL、boolean、`updated_at`の存在は調査の手掛かりになる。個々の設計意図と使用状況を照合する。確定済みの事実、下書き、現在状態、キャッシュを区別する。

収集・判定スクリプトは単独で動作する。既存の性能診断skillが使える場合は待機の時系列を、負荷検証skillが使える場合は変更前後の整合性と応答時間を照合する。セキュリティ診断skillが使える場合は分割先の権限・RLSも確認する。
