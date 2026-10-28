# 収集・拡張・版の差分

## 接続と観測窓

`collect.py`はPython標準ライブラリと`psql`を使う。接続情報は`PGSERVICE`またはlibpqの環境変数／パスワードファイルで指定する。`--psql`で実行ファイルを指定できる。収集するDBごとに実行し、別DB・別インスタンスのデータを混ぜない。

```text
python scripts/collect.py --instance-id prod-primary --output before.json
python scripts/collect.py --instance-id prod-primary --output after.json
python scripts/analyze.py before.json after.json --format markdown --output interval.md --top 20
```

上記はスキルフォルダからの実行例。間隔は処理の周期と目的から決める。正常時と繁忙時、日次／月次処理を区別し、秒単位の短い窓で長期傾向を断定しない。障害中のactivity／locksは時間間隔を明示した複数snapshotで捕捉する。

クエリ時間の上限には`--statement-timeout-ms`（既定10000ms）と`--lock-timeout-ms`（既定1000ms）、件数には`--row-limit`（既定10000件）を使う。上限到達は「全体を取得済み」ではない。収集自体の負荷と所要時間を確認し、対象・間隔を調整する。各ビューは異なる時点で読まれるため、ファイル全体を原子的なDB状態と解釈しない。出力先は新しいファイルを指定する。既存スナップショットは上書きしない。

収集自体もSQLを実行するため統計に影響する。識別できる収集SQLを除外しても、`BEGIN`・`SET`・`COMMIT`等のutility statementが`pg_stat_statements`に残る場合がある。高calls候補をアプリの処理と対応させるときは、収集間隔・時刻・tracking設定を確認する。これを消すために統計をresetしない。

累積統計は「どのSQLが時間を使ったか」を示すが、その時間の待機内訳は示さない。症状が出ている間は`scripts/sample_activity.py`で待機をサンプリングし、snapshotの区間と時刻を揃えて照合する。接続・権限・`--docker-container`の扱いは`collect.py`と同じ。手順と解釈は[wait-events.md](wait-events.md)を参照。

ローカル検証用の既存コンテナには`--docker-container NAME`を指定できる。この場合はコンテナ内の`psql`とlibpq環境を使い、ホストの`PG*`環境変数は引き継がない。接続先が意図した検証DBであることを確認する。

## 最初に残す情報

| 情報 | 用途 |
| --- | --- |
| インスタンス識別子、DB、server_version_num、実ビルド、拡張版、primary／standby | 比較対象と互換性 |
| 採取日時、時刻の基準、サーバ起動時刻、統計reset、収集の成否・上限 | 区間の有効性 |
| track_counts、track_activities、track_io_timing、track_wal_io_timing、pg_stat_statements設定 | 0と未計測の区別 |
| スキーマ、サイズ、索引・制約、パーティション、データ分布、更新頻度 | SQLの原因確認 |
| CPU、RAM、ストレージ性能／容量、クラウド・コンテナ制限 | DB外の資源制約 |
| SLO、代表負荷、デプロイ・DDL・設定変更、pool／ORM／trace | 利用者影響と前後比較 |

同じ設定値が2時点で一致しても、間で一時変更されていない証明にはならない。変更履歴を併せて確認する。

## pg_stat_statementsを使う

既存状態を確認する。

```sql
SHOW shared_preload_libraries;
SHOW compute_query_id;
SELECT extname, extversion, extnamespace::regnamespace
FROM pg_catalog.pg_extension
WHERE extname = 'pg_stat_statements';
```

未導入なら提供環境の拡張対応と作業権限を確認し、**既存値を保持したまま**`shared_preload_libraries`へ`pg_stat_statements`を追加する。追加にはサーバ再起動が必要。`compute_query_id`は通常`auto`または`on`を使うが、第三者のquery ID計算モジュールと競合させない。再起動後、統計を読むDBで次を実行する。

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

このDDLを収集スクリプトは自動実行しない。`pg_stat_statements`はクラスタ全体の統計を保持する一方、SQLオブジェクトの作成はDBごと。拡張のスキーマを`public`と仮定せず検出する。

他ユーザーのSQLやquery IDの表示には追加権限が必要になる。必要な場合はDB管理者と`pg_read_all_stats`等を検討する。このロールは他セッションの情報にもアクセスできるため、単なる接続権限と同じ扱いにしない。権限不足なら欠落範囲を報告し、取得できた統計を使う。

## 差分を信用できる条件

- `pg_stat_statements`はインスタンスと版に加え、`(dbid, userid, queryid, toplevel)`で照合する。queryidをメジャー版間・別クラスタ間で同じSQLの保証に使わない。
- 累積カウンタの差分だけを使う。平均・最小・最大・標準偏差を減算しない。`stats_since`、`minmax_stats_since`、全体reset、dealloc等の取得可能な情報で不連続を検知する。
- 2時点の両方にないentry、同じOIDでも再作成されたrelation、負の差分、期間0、resetを跨ぐカウンタを通常の順位表に混ぜない。再取得・追い出しによる欠損を開示する。
- テーブル／索引の個別統計resetは、利用できるマーカーだけでは全て検知できない。減少がなくてもreset後に値が追い越すケースがあるため、運用履歴が必要。
- 累積統計は非同期更新。長い収集トランザクションのcached snapshotに注意する。短いトランザクションで採取し、採取中の負荷もゼロではないことを記録する。

`analyze.py`が組み合わせを拒否した場合は、エラーの対象と再採取の条件を報告する。古いsnapshotの起動時刻や識別子を新しいものへ書き換えて比較を通さない。最新snapshotのactivity／locks／slot状態等は、取得時点の証拠として独立に確認できる。新しい起動・役割の中で少なくとも2時点を取り、累積統計の比較を再開する。

## 計画とログを追加する

スロークエリ、temp、lock wait、deadlock、autovacuum、checkpointを目的に応じて採取する。`jsonlog`または`csvlog`は構造化解析に適する。これらのログ出力には`logging_collector`等の前提があるため、既存のmanaged DBログ取得方法も確認する。

DB・user・PID・session ID・application_name・query ID・時刻を組み合わせる。query IDだけでは個別実行を識別できない。`log_min_duration_statement`等の閾値・sample rateはSLOとログ量で決める。全SQL・全パラメータの常時記録を既定にしない。

`auto_explain`はSQL拡張オブジェクトを作る方式ではなく、`LOAD`／session preload／shared preloadで読み込むモジュール。限定したセッションでの採取例:

```sql
LOAD 'auto_explain';
SET auto_explain.log_min_duration = '500ms';
SET auto_explain.log_format = 'json';
SET auto_explain.log_parameter_max_length = 0;
SET auto_explain.log_analyze = on;
SET auto_explain.log_timing = off;
SET auto_explain.log_buffers = on;
SET auto_explain.sample_rate = 0.1;
```

値は採取方法の例で、推奨の固定閾値ではない。権限・対象・期間・保存先・ログ量を確認して使用する。`log_analyze`は計測負荷があり、`log_timing=off`でも計測が無料になるわけではない。終了時に設定を戻す。パラメータ記録を止めてもSQLリテラルや計画に秘密情報が残る場合がある。

`scripts/analyze_logs.py LOG --format markdown --output REPORT`はPostgreSQLのJSONログをオフライン解析する。`--max-events`（既定1000件）は出力するイベント数の上限、`--include-details`はメッセージ・statement・detailの出力を許可する指定。stderrやCSV形式は形式を確認してから別途構造化する。ログ出力がないことを事象がなかった証拠にしない。採取閾値やサンプリングが不明なログから、全体のpercentileを推定しない。

メッセージ文字列によるtemp・duration等の分類は英語ログを対象とする。deadlockはSQLSTATE `40P01`でも検出する。ローカライズされたメッセージや未知の形式を未分類のまま残した場合、その制限を報告する。parse／bind／execute等のdurationを同一実行の独立サンプルとして足し合わせない。

## 保存と共有

SQL本文は`--include-query-text`で必要な時だけ収集する。本文を省いてもDB・relation・user・application_name等は残り得る。ログ、計画、DDL、パラメータ、収集結果のアクセス範囲と保持期間を決める。外部共有前にマスクし、issueやレポートに生の本番データを必須としない。

## 公式資料

- [pg_stat_statements](https://www.postgresql.org/docs/18/pgstatstatements.html)
- [累積統計](https://www.postgresql.org/docs/18/monitoring-stats.html)
- [統計設定](https://www.postgresql.org/docs/18/runtime-config-statistics.html)
- [ログ設定](https://www.postgresql.org/docs/18/runtime-config-logging.html)
- [auto_explain](https://www.postgresql.org/docs/18/auto-explain.html)
