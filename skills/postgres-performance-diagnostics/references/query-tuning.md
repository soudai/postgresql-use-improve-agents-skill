# SQL・索引・実行計画の診断

## 候補の順位と必要な情報

区間の累積実行時間、calls、1回当たりの実行時間、バッファアクセス、temp量を別々に見る。累積時間の大きいSQL、単発で利用者を長く待たせるSQL、短いが大量実行されるSQLでは対策が異なる。

対象SQLのスキーマ、代表パラメータと型、行数・分布、既存索引、制約、パーティション、関数・トリガーを確認する。正規化SQLは具体的な値の偏りを失っているため、同じqueryidの代表計画1件で全実行を代表させない。

候補が絞れたら、スキルフォルダから対象relationのメタデータと列・拡張統計を調べる。

```text
psql -X -qAt --set ON_ERROR_STOP=1 --set relation=public.orders --file sql/inspect_relation.sql
```

`public.orders`を対象のスキーマ修飾名へ置き換える。このSQLは全体収集マニフェストとは別の、対象を絞った調査用。relation名はpsql変数として渡し、SQL文字列に直接連結しない。スクリプトは読み取り専用トランザクション、statement timeout 10秒、lock timeout 1秒で実行し、JSONオブジェクトを1件返す。

列の型、統計ターゲット、null率、平均幅、distinct推定、相関、MCV頻度、拡張統計の定義情報と参照可能な統計、索引・制約メタデータを確認する。`n_distinct`の負値は行数に対するdistinct数の割合を表し、不正値とは限らない。列の`correlation`は物理的な行順との関係であり、列間相関そのものではない。このSQLはMCV／histogramの実データ値、default、索引・制約・拡張統計の式やpredicateの本文を省き、式や部分条件の有無を示す。式そのものが診断に必要なら、共有範囲を限定して別途確認する。

統計データが返らない理由には、未ANALYZE、統計の無効化、列・表への権限不足、RLSによる表示制限、拡張統計データの所有者による表示制限がある。`visibility`と各項目の`stats_status`を読み、統計が見えないことと定義が存在しないことを区別する。定義・ANALYZE履歴・実行ロールを確認する。値の偏りやprepared statementの比較に必要な代表値は、この出力とは別に、必要最小限を秘密情報として扱って収集する。

## 実行計画を読む

取得済みJSON計画は次で解析できる。

```text
python scripts/analyze_plan.py plan.json --format markdown --output plan-report.md
```

式や条件を出力したい場合は、共有範囲を確認して`--include-details`を追加する。計画を追加取得するときはまず通常の`EXPLAIN (SETTINGS, FORMAT JSON)`を検討する。実測が必要なら、対象SQLの実行権限と負荷・副作用を評価した上で、例えば次のオプションを使う。

```sql
EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)
SELECT ...;
```

PostgreSQL 18以降は`ANALYZE`指定時に`BUFFERS`が既定で有効。`SELECT ...`は実行対象SQLへ置き換える。実行前に`statement_timeout`と`lock_timeout`、キャンセル方法を決める。更新SQLは実際に更新を行い、SELECTでも関数が副作用を持ち得る。検証DBを使えるなら同じデータ特性で再現する。単にBEGIN／ROLLBACKで囲むだけでは全副作用を抑止できない。

計画は次の順に調べる。

1. 最終結果と途中結果の行数、初期化・反復回数、フィルタによる除外量を確認する。`Actual Rows`や時間の表示はloops当たりである場合があるため、その計測単位を確認する。
2. 大量アクセスがあるノードから、スキャン、JOIN、Sort／Hash、集約へ遡る。包含関係のあるノード時間を合算して総時間を水増ししない。並列workerの数と役割も考慮する。
3. 実測と推定の乖離、バッファ使用、temp、Heap Fetches、worker planned/launchedを確認し、仮説を絞る。

`analyze_plan.py`の`hotspots`は、各ノードの`Actual Total Time × loops`から子ノード分を引いた自己時間の上位。手順2の出発点に使う。parallel workerのノードは各プロセスの合計になり実時間を超え得る。CTE・SubPlanの再利用でも近似になる。

自動候補のうち、`repeated_seq_scan`はNested Loopの内側で繰り返すSeq Scan、`lossy_bitmap`は`work_mem`を超えてページ単位の再検査になったBitmap Heap Scan、`index_only_heap_fetches`はvisibility mapが古くheapを読んだIndex Only Scan、`planning_dominates`・`jit_overhead`は計画・JITの時間が実行に比べて大きい計画を示す。

LIMITによって上位ノードが途中で処理を止めた場合や、実行されなかった枝では、通常の全件実行と同じ条件で行数を比較できない。実測がない計画は、実行時間・spill・実際の読み取り量を証明しない。

## スキャンと索引

小さな表、全件に近い取得、一括集計ではSeq Scanが適切になり得る。`seq_scan`や`idx_scan`は表／索引の集約統計であり、特定SQLのアクセス方式・コストを確定するものではない。

少数行のための大量走査なら、選択性、式、型変換、照合順序、統計鮮度、partition pruningを確認する。Index Only ScanのHeap Fetchesが多ければvisibility mapとVACUUM状況を調べる。索引があっても大量のrandom accessで悪化する場合がある。

JOINでは内側アクセスの反復数と返却量、ORDER BY／LIMITでは必要行に達するまでの走査量を見る。候補索引の列順、部分条件、式、INCLUDEを、検索条件と並び順に対応させる。PostgreSQL 18のB-tree skip scanを考慮し、「先頭列の等価条件がないから使用不能」と決めない。Hash JoinやSortの存在自体を索引不足としない。

索引候補には、容量、INSERT／UPDATE／DELETEとWALの追加コスト、制約との関係、作成中の負荷・ロック、失敗時の扱いを添える。`CREATE INDEX CONCURRENTLY`にも制約と負荷がある。重複／未使用候補は、十分な観測期間、稀なバッチ、standbyでの利用、制約依存を確認してから扱う。`idx_scan=0`だけで削除しない。

`analyze.py`の`redundant_indexes`は、同じテーブルで別索引のキー列の先頭と一致する非uniqueのB-tree索引を示す。式・部分索引・INCLUDE付き・制約用の索引は対象外。短い索引の方が小さく速い場合や、standby・稀なバッチだけが使う場合があるため、削除前に計画への影響を確認する。

## 代表的な書き換え

計画と代表パラメータで原因を確認してから適用する。結果の同値性（NULL、重複、並び順）を検証する。

| 計画・SQLの特徴 | 原因の候補 | 比較する対策 |
| --- | --- | --- |
| 深い`OFFSET`で読み捨てが大きい | OFFSET分を毎回読む | 並び順キーによるkeyset pagination |
| 列に関数・型変換があり索引を使わない | 式と索引の不一致、暗黙の型変換 | パラメータ型を列に合わせる、式索引 |
| 異なる列の`OR`で全件走査 | 単一の索引条件にできない | BitmapOrになる索引、`UNION ALL` |
| `LIKE 'abc%'`で索引を使わない | 非Cの照合順序 | `text_pattern_ops`の索引、`pg_trgm` |
| `LIKE '%abc%'`、類似検索 | B-treeでは絞れない | `pg_trgm`のGIN／GiST索引 |
| `NOT IN (subquery)` | anti joinにできず、hashed SubPlanが`work_mem`に収まらないと行ごとに再実行される | NULLを含む場合の意味の違いを確認した上で`NOT EXISTS` |
| 巨大な`IN`リストやリテラル埋め込み | 計画時間、queryidの分散 | `= ANY($1::type[])`、一時テーブル／`VALUES`とのJOIN |
| 相関サブクエリが行ごとに実行される | SubPlanの反復 | JOIN、`LATERAL`、集約の先行 |
| `count(*)`の正確な件数を頻繁に取得 | 全件走査 | 要件の確認、推定値、集計テーブル |
| 大量の単一行INSERT／UPDATE | 往復とcommitの回数 | バッチ化、`COPY`、`INSERT ... SELECT` |

## 大量取得とN+1

`Δrows / Δcalls`は返却／更新行数であり、走査行数ではない。SELECTとDMLを分け、行幅、転送バイト数、アプリが使う行・列の割合を見る。大量処理の仕様を確認してから、ページング、列削減、集約、バッチ化を比較する。

高callsは正常な高トラフィックでも起こる。N+1を確認するには、同一リクエスト／ジョブで親の件数に応じて同形SQLが反復するtraceと呼出箇所を使う。証拠がない場合は「N+1候補」とし、必要なtraceを指定する。

eager loading、JOIN、バッチ取得は結果の多重化・順序・整合性・アプリメモリ使用まで検証する。評価指標はSQL回数／request、転送量、API時間、DB累積負荷。DB実行時間だけで通信や結果変換を含む利用者の応答時間を説明しない。

## 見積もりと計画退行

古い統計、MCV／histogramで表せない偏り、列間相関、統計ターゲットを確認する。ANALYZE、列単位の統計ターゲット、CREATE STATISTICSは対象と費用を明示して提案する。拡張統計ですべてのJOIN推定誤差が解決するとは扱わない。

prepared statementではgeneric／custom plan、パラメータ分布、型を確認する。`plan_cache_mode`の全体変更で特定例だけを直さない。DDL、設定、デプロイ、統計更新の前後で、同じqueryidでも計画が変わることを考慮する。

計画時間、JIT時間、parallel worker不足、関数・トリガー内部の負荷も候補になる。計画時間計測が無効な場合の0を「planning負荷なし」としない。関数内SQLは必要に応じて追跡設定やauto_explainのnested statement採取を検討する。

## 検証例

| ケース | 求める判断 |
| --- | --- |
| 小表のSeq Scan | 回数だけで異常にしない |
| 少数行返却・大量フィルタ除外 | 選択性、式、索引、分布を調べる |
| ORDER BY LIMITの深い走査 | 代表値ごとに索引／SQL修正を比較 |
| 相関列の誤推定 | 統計と実測を突き合わせる |
| 同形SQLの大量callsのみ | N+1確定にせずアプリtraceを求める |
| generic planが一部値で退行 | 代表パラメータ分布全体で評価 |

## 公式資料

- [EXPLAINの解釈](https://www.postgresql.org/docs/18/using-explain.html)
- [EXPLAINオプション](https://www.postgresql.org/docs/18/sql-explain.html)
- [複合索引とskip scan](https://www.postgresql.org/docs/18/indexes-multicolumn.html)
- [ORDER BYと索引](https://www.postgresql.org/docs/18/indexes-ordering.html)
- [プランナー統計](https://www.postgresql.org/docs/18/planner-stats.html)
- [PREPARE](https://www.postgresql.org/docs/18/sql-prepare.html)
