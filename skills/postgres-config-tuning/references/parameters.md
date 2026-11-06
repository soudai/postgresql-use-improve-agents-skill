# パラメータ別の考え方

値は出発点。環境とワークロードで検証して決める。「反映」は`pg_settings.context`による（postmaster=再起動、sighup=reload、user=reload／ロール／セッション）。実サーバの`pg_settings`で単位・既定値・有効な値を確認する。

## メモリ

| パラメータ | 出発点 | 考え方・注意 |
| --- | --- | --- |
| shared_buffers | 専用サーバでメモリの約25% | 40%を超えても効果は小さいことが多く、OSキャッシュとの二重キャッシュが増える。再起動が必要。小さすぎる兆候は、論理読み取りの多い処理での`IO:DataFileRead`や`LWLock:BufferMapping`の多さ |
| huge_pages | shared_buffersが数GB以上なら`on`を検討 | OSで`vm.nr_hugepages`を確保してから。`on`は確保不足だと起動しない。PostgreSQL 17以降は`huge_pages_status`で実際の使用を確認できる |
| effective_cache_size | メモリの50〜75% | プランナへの見積もりで、メモリを確保しない。索引走査の評価に影響する |
| work_mem | 同時実行から逆算した上限以下 | ソート・ハッシュ等の1ノードごとの上限。1クエリで複数ノード、parallel worker、hashでは`hash_mem_multiplier`倍まで使う。全体の既定値は控えめにし、spillするロール・処理だけ`ALTER ROLE ... SET`や`SET LOCAL`で上げる |
| hash_mem_multiplier | 既定2.0 | hash集約・hash joinのspillが多いときに、work_memを全体で上げる代わりに検討する |
| maintenance_work_mem | メモリの5%程度、256MB〜2GB | VACUUM・CREATE INDEXで使う。PostgreSQL 17以降はVACUUMのdead tuple格納に1GBの上限がない |
| autovacuum_work_mem | 既定-1（maintenance_work_memを使用） | autovacuum workerごとに使う。worker数との積で予算を確認する |
| temp_file_limit | 既定-1（無制限） | 暴走クエリのtempでディスクが埋まるのを防ぐ。正当な大規模処理の上限も考慮する |

work_memの上限の目安:

```text
(PostgreSQLに使えるメモリ × 0.8 − shared_buffers)
  ÷ (同時にactiveなセッション数 × 2ノード × hash_mem_multiplier)
```

同時activeセッション数は、poolのサイズや観測した平均・ピークのアクティブセッション数から決める。`max_connections`で割ると過小評価になりやすい。OOM killerが動く環境では余裕を残す。

## 接続

`max_connections`はメモリ（接続ごとの固定領域とlock table）と、snapshot取得のコストに関わる。同時にactiveなセッションをCPUコア数の数倍程度に抑えると、throughputが安定しやすい。接続数が多い場合は、設定値を上げる前にPgBouncer等のconnection poolerを検討する。`max_connections`の変更は再起動が必要で、standbyではprimary以上の値が必要。

## WALとチェックポイント

| パラメータ | 出発点 | 考え方・注意 |
| --- | --- | --- |
| max_wal_size | 既定1GB。WAL量に合わせる | checkpointの多くが`num_requested`（WAL量による）なら小さすぎる。目安は`WAL生成速度 × checkpoint_timeout × 2`。pg_walの容量と、クラッシュ復旧時間が増える |
| min_wal_size | 既定80MB | WALファイルの再利用量。WAL量が周期的に急増する環境で上げる |
| checkpoint_timeout | 既定5min。書き込みの多い環境では15min程度 | checkpoint後の最初の更新でfull-page imageが書かれる。間隔を延ばすとFPIとI/Oが減り、復旧時間が延びる |
| checkpoint_completion_target | 0.9（14以降の既定） | 書き込みをcheckpoint間隔に分散する |
| wal_compression | `lz4`または`zstd` | FPIを圧縮し、WAL・アーカイブ・レプリケーション量を減らす。CPUを使う。ビルドの対応を確認 |
| wal_buffers | 既定-1（shared_buffersの1/32、最大16MB） | `pg_stat_wal.wal_buffers_full`が増え続ける場合に64MB程度へ。再起動が必要 |
| synchronous_commit | `on` | `off`は直近のcommitを失い得る（破損はしない）。許容できる処理だけトランザクション・ロール単位で使う |
| fsync, full_page_writes | `on` | 性能目的で`off`にしない。データ破損の危険がある |

## プランナとI/O

| パラメータ | 出発点 | 考え方・注意 |
| --- | --- | --- |
| random_page_cost | SSD 1.1、ネットワークストレージ 1.1〜2.0、HDD 4.0 | 既定4.0は回転ディスクを想定する。変更は多くの計画を同時に変えるため、代表SQLの計画を前後比較する。テーブル空間単位でも設定できる |
| effective_io_concurrency | PostgreSQL 18の既定16。SSDで64〜200 | bitmap heap scan、先読み、非同期I/Oの並行度 |
| maintenance_io_concurrency | PostgreSQL 18の既定16 | VACUUM等の先読みの並行度 |
| io_method | PostgreSQL 18の既定`worker` | `io_uring`はLinuxの対応ビルドで利用可能。`sync`は従来の同期I/O。再起動が必要。変更は前後測定する |
| io_workers | 既定3 | `io_method=worker`のworker数。I/Oの多い環境とCPUに応じて調整する |
| default_statistics_target | 既定100 | 全体を上げると統計の取得・計画が重くなる。推定誤差のある列だけ`ALTER TABLE ... ALTER COLUMN ... SET STATISTICS`で上げる |
| jit | OLTPでは`off`または`jit_above_cost`を上げる | 短いクエリでコンパイル時間が実行時間を上回ることがある。`pg_stat_statements`のjit時間で判断する |
| plan_cache_mode | 既定`auto` | 全体変更ではなく、問題のあるprepared statementの利用箇所で対処する |

## 並列実行

`max_worker_processes`は並列クエリ、論理レプリケーション、拡張のbackground workerで共有する上限（再起動が必要）。`max_parallel_workers`はその中の並列クエリの上限で、CPU数を超えない。`max_parallel_workers_per_gather`はOLTPでは既定2のまま、同時実行の少ない分析処理で増やす。workerはそれぞれwork_memを使う。`max_parallel_maintenance_workers`はCREATE INDEX等で使う。

## autovacuum

| パラメータ | 出発点 | 考え方・注意 |
| --- | --- | --- |
| autovacuum | `on` | 無効化しない。特定テーブルだけ止める場合もwraparound対策は動く |
| autovacuum_max_workers | 既定3。大規模・多テーブルで5〜6 | PostgreSQL 18は`autovacuum_worker_slots`（再起動）までreloadで変更できる |
| autovacuum_vacuum_cost_limit | 既定-1（vacuum_cost_limit=200を使用）。SSDで1000〜2000 | 全workerで共有する予算。workerを増やすだけでは1つあたりが遅くなる |
| autovacuum_vacuum_cost_delay | 2ms（12以降の既定） | 20ms等の古い値は処理を大きく抑制する |
| autovacuum_naptime | 既定1min | 多数のDBがある場合、各DBの巡回間隔に影響する |
| autovacuum_vacuum_scale_factor等 | 全体は既定、巨大・高更新テーブルはテーブル単位で下げる | `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = ...)`。PostgreSQL 18は`autovacuum_vacuum_max_threshold`（既定1億行）で巨大テーブルの閾値に上限がある |
| track_cost_delay_timing | PostgreSQL 18 | cost delayで待った時間を進捗ビュー・ログで確認できる。計測負荷を`pg_test_timing`で確認 |

autovacuumが追い付かない原因が、古いsnapshot・replication slot・prepared transactionによる回収不能なら、設定変更では解決しない。

## 観測とログ

| パラメータ | 推奨 | 理由 |
| --- | --- | --- |
| shared_preload_libraries | `pg_stat_statements`を既存値に追加 | SQL別統計。再起動とCREATE EXTENSIONが必要 |
| compute_query_id | `auto`または`on` | statistics・activity・ログをquery_idでつなぐ |
| track_io_timing | `on` | I/O時間の計測。`pg_test_timing`で時刻取得の負荷を確認 |
| track_wal_io_timing | 必要時に`on` | WAL I/O時間 |
| log_min_duration_statement | SLOに基づく値。量が多ければ`log_min_duration_sample`と`log_statement_sample_rate` | 遅いSQLの記録。全SQLの記録を既定にしない |
| log_checkpoints | `on`（15以降の既定） | checkpointの原因と時間 |
| log_lock_waits | `on` | `deadlock_timeout`を超えたロック待ち |
| log_temp_files | `0`または数MB以上 | spillしたSQLの特定 |
| log_autovacuum_min_duration | 数秒〜（15以降の既定10min） | 遅いautovacuumと原因 |
| track_activity_query_size | 4kB以上 | activityでのSQLの切り詰め防止。再起動が必要 |

## タイムアウトと安全網

`idle_in_transaction_session_timeout`、`transaction_timeout`（PostgreSQL 17以降）、`statement_timeout`、`lock_timeout`はアプリケーションロール単位で設定する。全体に設定すると、保守・マイグレーション・バッチも切断する。`max_prepared_transactions`は二相コミットを使わない限り0にする。残留したprepared transactionはロックとVACUUMを妨げる。

## managed service

Amazon RDS／Aurora、Cloud SQL、Azure Database for PostgreSQL等は、パラメータをparameter group・フラグで変更する。`shared_buffers`等はインスタンスサイズの式で決まっていることが多い。`ALTER SYSTEM`や一部のパラメータは使えない。再起動の要否、適用タイミング（即時／再起動時）、提供者固有のパラメータを仕様で確認する。

## 公式資料

- [Server Configuration](https://www.postgresql.org/docs/18/runtime-config.html)
- [Resource Consumption](https://www.postgresql.org/docs/18/runtime-config-resource.html)
- [Write Ahead Log](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- [WAL Configuration](https://www.postgresql.org/docs/18/wal-configuration.html)
- [Query Planning](https://www.postgresql.org/docs/18/runtime-config-query.html)
- [Automatic Vacuuming](https://www.postgresql.org/docs/18/runtime-config-autovacuum.html)
- [Huge Pages](https://www.postgresql.org/docs/18/kernel-resources.html#LINUX-HUGE-PAGES)

## 総メモリの見積もり

ホストの`--ram-gb`とcgroup／サービスの`--memory-limit-gb`の小さい方を使う。`--max-active-connections`、`--sort-nodes`、`--hash-nodes`、`--parallel-workers-per-query`、`--maintenance-jobs`に同時に重なる量を入力する。leaderとworkerに演算の予算を配り、保守メモリは同時コマンドとautovacuum workerごとに確保する。並列保守コマンドのmaintenance_work_memはコマンド全体の予算とする。20%の余力と共有領域を確保した残額から概算する。共有ハッシュ領域、バックエンドの固定費、OSキャッシュ等は実測で確認する。計画やピーク負荷の入力がない場合は、レポートの仮定と不足情報を確認する。
