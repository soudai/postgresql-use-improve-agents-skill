# 変更の適用・検証・切り戻し

## 反映方法を選ぶ

| 範囲 | 方法 | 注意 |
| --- | --- | --- |
| インスタンス全体 | `postgresql.conf`、または`ALTER SYSTEM SET`（`postgresql.auto.conf`に書かれ、confより優先） | 構成管理と二重管理にならないよう、どちらで管理するか決める |
| DB単位 | `ALTER DATABASE db SET name = value` | 新しいセッションから有効 |
| ロール単位 | `ALTER ROLE app SET name = value`、`ALTER ROLE app IN DATABASE db SET ...` | アプリ・バッチ・保守のロールを分けると安全に差を付けられる |
| トランザクション・セッション | `SET LOCAL`／`SET` | 対象の処理だけに限定できる |
| テーブル単位 | `ALTER TABLE ... SET (autovacuum_... = ...)`、`ALTER TABLESPACE ... SET (random_page_cost = ...)` | 全体の既定値を変えずに例外を作る |
| managed service | parameter group・フラグ | 適用タイミングと再起動の要否は提供者の仕様に従う |

優先順位はトランザクション・セッション＞ロール＋DB＞ロール＞DB＞`postgresql.auto.conf`＞`postgresql.conf`。変更後に`pg_settings.source`で、どこから値が来ているか確認する。

## 適用手順

1. 変更前の`settings.json`と、比較用の指標（区間レポート、待機サンプル、API応答時間）を保存する。
2. 変更内容を、根拠・期待する効果・確認指標とともに記録する。1回の変更は、効果を切り分けられる単位にする。
3. 反映する。reloadで有効なものは`SELECT pg_reload_conf();`、再起動が必要なものは保守時間に再起動する。
4. 反映を確認する。

   ```sql
   SELECT name, setting, unit, source, pending_restart
   FROM pg_settings WHERE name IN ('work_mem', 'max_wal_size');
   -- 設定ファイルの構文エラーや無効な値（スーパーユーザー等の権限が必要）
   SELECT sourcefile, sourceline, name, setting, error
   FROM pg_file_settings WHERE error IS NOT NULL;
   ```

   `pending_restart = true`は再起動まで反映されていない。設定ファイルの誤りがあるとreloadでその値が無視される。
5. 同じ負荷条件で比較する。

## 確認する指標

| 変更 | 効果の確認 | 退行の確認 |
| --- | --- | --- |
| shared_buffers | 読み取りI/O、`IO:DataFileRead`の割合、応答時間 | OSのメモリ・swap、checkpointの書き込み量 |
| work_mem等 | temp量、対象SQLの時間 | プロセスのRSS、OOM、swap |
| max_wal_size／checkpoint | `num_requested`の比率、WAL量、FPI、書き込みI/O | pg_walの使用量、復旧時間の見積もり |
| random_page_cost等 | 代表SQLの計画と時間 | 他SQLの計画退行（`pg_stat_statements`の区間比較） |
| autovacuum | dead tuple、autovacuum backlog、vacuum所要時間 | I/O遅延、応答時間 |
| jit | JIT時間、対象SQLの時間 | 長い分析SQLの時間 |
| ログ設定 | 必要なイベントが記録される | ログ量、ディスク、ログ収集の遅延 |

## 切り戻し

- `ALTER SYSTEM RESET name;`（または該当行を戻す）→ `SELECT pg_reload_conf();`、再起動が必要なものは再起動。
- `ALTER ROLE app RESET name;`、`ALTER DATABASE db RESET name;`、`ALTER TABLE ... RESET (...)`。
- 再起動が必要な値（shared_buffers、huge_pages等）は、起動できない設定に備えて、変更前の設定ファイルを残し、起動確認まで保守時間内で行う。`huge_pages=on`はOS側の確保不足で起動に失敗する。
- 切り戻し条件（応答時間、エラー率、メモリ使用率の閾値）を変更前に決める。
