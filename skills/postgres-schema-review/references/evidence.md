# 入力と収集

## 読み取り収集

Python 3.10以降とpsqlを使用する。接続先はPGSERVICEまたはlibpqの環境設定、秘密は既存のパスワードファイル等で渡す。

```sh
python scripts/collect.py --schema public --output snapshots/schema.json
python scripts/review.py snapshots/schema.json --contract contract.json --evidence evidence.json --format markdown --output reports/schema.md
python scripts/review.py snapshots/schema.json --contract contract.json --evidence evidence.json --format json --output reports/schema.json
```

パスはskillのディレクトリを基準にした例。正確な引数と既定上限は各スクリプトの `--help` を使う。対象版はPostgreSQL 18・19で、実サーバの版とpreviewかどうかを確認する。別DBは別snapshotとして収集する。

catalogのテーブル・列・制約・索引と、見える範囲のNULL統計を取得する。収集は読み取りtransaction、statement/lock timeoutと件数上限を使う。アプリテーブルの走査、関数・policyの試験実行、ANALYZE、DDLは行わない。CHECK・default・索引の式、SQL本文、MCV・histogramは出力しない。名称も内部情報になり得るため、出力の共有範囲は依頼に合わせる。

各sectionの `status`、`truncated`、`rows` を維持する。取得失敗や上限超過を空の正常結果に置き換えない。statisticsは権限・ANALYZEの有無・RLS等で見えない列があり、catalogを全件取得しても全列の分布を確認した意味にはならない。

## snapshot形式

`schema_version: 1`、`metadata`、`sections` を持つJSON。sectionは `tables / columns / constraints / indexes / statistics`。各sectionは `{"status":"ok","truncated":false,"rows":[]}` を基本とする。

- tables: `relation, schema, name, kind, partition`。通常表とpartitioned tableを対象とする。
- columns: `relation, name, type, not_null, domain_has_check, has_default, identity, generated`。
- constraints: `relation, name, type, columns, referenced_relation, referenced_columns, validated, enforced, match_type, update_action, delete_action`。
- indexes: `relation, name, columns, unique, valid, partial, expression`。
- statistics: `relation, column, null_frac`。統計上の推定で、列の必須性を保証しない。

`relation` はcatalogが出力したschema修飾識別子をそのまま使用する。例は `public.orders`、大文字やドットを含む名前は `public."Order.Items"`。文字列をドットで分割しない。FKの列配列は対応順を保持する。制約typeは `p/u/f/c/n`、参照動作は `a/r/c/n/d`（NO ACTION/RESTRICT/CASCADE/SET NULL/SET DEFAULT）、MATCHは `s/f/p`。

既存DDLだけを使う場合は、許可された隔離DBへ適用して収集するか、確認できた事実をこの形式に変換する。入力値の式を実行しない。未知の属性を推測で補わず欠測として記録する。not_nullは列とdomain階層のNOT NULLを反映する。domain_has_checkはdomain階層にCHECKがあることだけを示し、式がNULLを禁止するかは追加確認する。

## 設計契約

[contractの例](../assets/contract.example.json)を出発点に、`schema_version: 1` と `tables` を用意する。各テーブルの主な項目:

| 項目 | 意味 |
| --- | --- |
| relation / source | catalogと一致する対象、業務資料・コードの出典 |
| row_meaning / kind | 1行が表すもの。kindはentity/event/projection/draft/cache |
| required_columns | 常に必須である列。状態依存の必須性は追加調査として業務資料とscopeに記録する |
| expected_name / column_names | 合意した表名、現列名から期待する列名への対応 |
| relationships | columns、referenced_relation、referenced_columns、enforce、allow_null、delete_action、update_action、match_type、source |
| column_groups | name、columns、lifecycle、writers、created_by、finalized_by、retention |
| immutable_after | 更新を禁止する確定時点・条件 |
| flags | columns、meaning（boolean/state/event/derived/soft_delete）、必要ならsource_column、違反判定のinvariant |
| exceptions | rule、reason、sourceを持つ明示的な例外 |

契約の出典がなければ解釈は未確定。外部ID・非同期整合性などでFKを設けない関連は `enforce:false` と理由の出典を記録する。この場合、外部の参照先名・列名が不明ならreferenced_relation/referenced_columnsを省略できる。例外は期限や再確認条件も業務資料に残す。用語集は `vocabulary.terms` と `vocabulary.allowed_abbreviations` を補助情報として保持できる。綴りの類似度から自動改名しない。

## 観測情報

[evidenceの例](../assets/evidence.example.json)の `writes / profiles / contention` を使う。空配列は未観測として扱い、正常の証明にしない。レポートのobservationsは入力レコード数であり、実際のSQL実行回数ではない。v1は入力された観測の照合を行う。リポジトリ全体の自動SQL解析や本番データの集計実行は提供しない。

- writes: `relation, operation, columns, business_operation, after_finalization, source, observed_at`。operationはINSERT/UPDATE/DELETE/TRUNCATE。ORM/migration/実SQLから、更新対象の列群と業務操作を対応付ける。ORMが全列更新する場合、実際の変更列はtraceや業務処理から補う。`after_finalization:true` は実行を観測し確定後だと確認した操作にだけ使う。静的コードから「起こり得る」と推測した処理は確定後の実行観測として扱わない。
- profiles: `relation, kind, columns, violations, rows_checked, scope, complete, source, observed_at`。kindはnull/flag_combination。flagの違反は契約のinvariantと対応付ける。violationsは業務契約に違反する件数で、単純なNULL総数やfalseの件数ではない。completeは記載したscopeの採取完了を表す。v1のrequired_columnsは常時必須だけを表すため、条件付き必須のNULL集計は要追加調査となり、状態条件を資料と照合してレポートへ補足する。
- contention: `relation, operations, same_key, wait_ms, blocker_chain, transaction_boundary, source, observed_at`。同じ対象キーの競合、業務操作、blockerの連鎖とtransaction境界を、時刻の揃ったtrace等で確認する。writesの業務操作・列群と同じtrace期間・transactionを照合する。transaction_boundary:trueはその対応を確認済みであることを表す。v1はtrace相関IDからこの対応を自動検証しない。値やキーそのものはこの形式へ転記しない。

sourceと観測時刻を伴う情報で確度を評価する。異なる環境・古いsnapshot・DDL変更前のtraceを混在させず、対象と観測期間をレポートに示す。統計収集のresetとデプロイの境界も確認する。

`pg_stat_statements`の集計では同じキーへの集中、transaction内の実行順序、実際の変更値は分からない。行ロックの待機はtransaction IDとして現れることがあり、`RowExclusiveLock`の保持件数は競合件数ではない。既存の活動サンプリング結果と `pg_blocking_pids()`、アプリtraceを組み合わせて入力を作る。[pg_stat_statements](https://www.postgresql.org/docs/18/pgstatstatements.html)、[pg_locks](https://www.postgresql.org/docs/18/view-pg-locks.html)

`pg_stats.correlation`は物理的な行順序と列値の関係で、列同士の共起ではない。状態別のNULLや列群の同時更新は、別途許可された範囲の集計・traceで確認する。[pg_stats](https://www.postgresql.org/docs/18/view-pg-stats.html)
