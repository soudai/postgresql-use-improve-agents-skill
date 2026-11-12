# 待機イベントのサンプリングと解釈

## なぜ最初に見るか

`pg_stat_statements`はSQLごとの累積時間を示すが、その時間がCPU・I/O・ロック・内部競合のどれに使われたかは示さない。`pg_stat_activity`を一定間隔でサンプリングすると、DB時間の内訳（平均アクティブセッション数と待機の割合）が分かり、調べる手順を選べる。症状が「全体が遅い」「時々詰まる」のときは、SQL別の順位より先にこれを取る。

## 採取する

```text
python scripts/sample_activity.py --instance-id prod-primary --samples 120 --interval 1 --output samples.json
python scripts/sample_activity.py --instance-id prod-primary --samples 60 --interval 1 --format markdown --output samples.md
```

1つの接続で、autocommitの読み取りSELECTと`pg_sleep`を交互に実行する。各SELECTは新しいトランザクションなので、activityを毎回読み直す。SQL本文は採取しない。`--include-sessions`は各サンプルのセッション（application_name、DB名、PID等）を残すため、必要時だけ使う。

- 症状が出ている時間帯に取る。正常時のサンプルも取ると比較できる。
- 間隔は通常1秒。1秒未満は観測負荷とデータ量が増えるため、短時間の再現時に限る。
- サンプル数が少ないと割合の誤差が大きい。数十サンプル未満の割合を断定に使わない。
- `Lock`待ちのセッションだけ`pg_blocking_pids()`を呼ぶ。待機者が多い障害時は、この呼び出し自体にも負荷がある。
- 1回のサンプルが間隔より長くかかった場合は、遅れを詰めて連続実行せず、次の間隔から採り直す。遅れた回数と全体の所要時間は`pacing`に残る。
- 中断（Ctrl+CやSIGTERM）しても、それまでのサンプルでレポートを書き、`error`に`interrupted`を残す。接続の失敗は`collect.py`と同じ固定の分類で示し、メッセージ本文は残さない。

## 出力を読む

| 項目 | 意味 |
| --- | --- |
| average_active_sessions / peak_active_sessions | 取得できた各時点のクライアント／parallel workerのactive数の平均／最大。観測範囲が不完全ならnull |
| observed_average_active_sessions / observed_peak_active_sessions | 観測できたactive数。権限不足・計測無効時は全体に対する下限 |
| activity_coverage / unknown_session_samples | 観測範囲と、状態・種類を判定できない延べセッション数 |
| wait_profile | active時間の待機内訳。`No reported wait event`は計測された待機イベントがない状態。CPU使用はOS指標で確認する |
| by_query_id | 時間を使っているquery_idと、その主な待機。`pg_stat_statements`のqueryidと照合する |
| lock_blockers | ロック待ちを生んだPIDと、待機者の観測回数 |
| idle_in_transaction | トランザクションを開いたまま待つセッション。最大経過秒 |
| background | autovacuum、checkpointer、walsender等の待機 |
| by_database / by_user / by_application | active時間のDB・ロール・application_name別の内訳と主な待機。どの利用者の負荷かを分ける |
| connections | client backend数の平均・最大と`max_connections` |
| timeline | 最大120区間に束ねた時系列。区間ごとの平均・最大active、最大Lock待ち、最大の判定不能数。ピークを平均で消さない |

サンプラーの出力はschema version 2。`track_activities`の取得漏れ・無効化や、state／backend_typeの非表示を検出するとAASとpeakは`null`になり、`warnings`に理由を残す。旧サンプルで設定を取得していない場合も未確認になる。非表示のセッションをbackgroundや負荷0として扱わない。`unknown_session_samples=0`でも設定が未取得なら完全な観測とはいえない。`wait_profile`と`by_query_id`のshareは、見えたactiveセッションだけを母数とする。

[統計の可視性](https://www.postgresql.org/docs/18/monitoring-stats.html#MONITORING-STATS-VIEWS)により、通常の利用者からは別ロールのセッション情報の一部が非表示になる。許可された監視ロール（`pg_read_all_stats`等）の利用を検討し、再採取で欠測が解消したか確認する。`track_activities`はバックエンドごとにも無効化できるため、採取側の設定がonでも他のセッションの`state=disabled`を調べる。権限変更や全体設定の変更を自動で行わない。

average_active_sessionsがCPUコア数を継続的に超え、`CPU`の割合が高ければCPU飽和を疑い、OSのCPU使用率・run queueで確かめる。待機イベントのないactiveはCPU以外の未計測の待ちも含む。

`pg_stat_database`の`active_time`差分から`analyze.py`が出す`average_active_sessions`はDB単位の区間平均で、サンプリングと独立に照合できる。

## 待機イベントの解釈

| 待機 | 主な原因の候補 | 次に読む |
| --- | --- | --- |
| CPU（待機なし） | 論理読み取りの多いSQL、計画時間、関数、JIT | `analyze.py`の`sql_shared_blks_accessed`、[query-tuning.md](query-tuning.md) |
| IO:DataFileRead / DataFilePrefetch | shared buffersにないページの読み取り。OSキャッシュで満たされることもある | [io-memory.md](io-memory.md)、`sql_shared_read_bytes` |
| IO:BufFileRead / BufFileWrite | sort／hashのtemp spill | [io-memory.md](io-memory.md#temp-spill) |
| IO:WALSync / WALWrite、LWLock:WALWrite / WALInsert | commitの多さ、WAL量、fsync遅延 | [maintenance-replication.md](maintenance-replication.md#walとチェックポイント) |
| IPC:SyncRep | 同期レプリケーションのcommit待ち | [maintenance-replication.md](maintenance-replication.md#レプリケーションとwal保持) |
| Lock:transactionid / tuple | 行ロック待ち。同じ行を更新するトランザクションの競合 | [locking.md](locking.md) |
| Lock:relation | DDL・LOCK TABLE・VACUUM FULL等の表ロック。DDLの待機列も含む | [locking.md](locking.md) |
| Lock:extend | 同じrelationへの同時大量INSERTによるファイル拡張の競合 | INSERTの同時実行数とバッチ化 |
| LWLock:LockManager | fast-pathに入らないロックの多さ。多数のパーティションや索引に触れるSQL | パーティションのpruning、索引数、`max_locks_per_transaction` |
| LWLock:BufferMapping | working setがshared buffersを超え、置換が多い | 論理／物理読み取り量、`pg_stat_io`のevictions |
| LWLock:BufferContent、IPC:BufferIO | 特定ページへの集中（同じ行の更新、B-treeの右端への挿入） | ホットな行・索引の設計 |
| LWLock:SubtransSLRU / SubtransBuffer | 1トランザクションで64を超えるsubtransaction（SAVEPOINT、PL/pgSQLのEXCEPTION）と長いトランザクションの組み合わせ | アプリのSAVEPOINT使用、例外ブロック |
| LWLock:MultiXact* | 外部キー検査や`FOR SHARE`による同じ行の共有ロックが多い | 更新パターン、MXID age |
| LWLock:ProcArray | 非常に多い接続でのsnapshot取得 | 接続数、pool |
| Client:ClientRead（active中） | クライアントからの次の入力待ち（COPY、拡張プロトコル） | アプリ・ネットワーク |
| Client:ClientWrite | クライアントが結果を受け取るのが遅い。大きな結果セット | 結果の行数・列、ネットワーク |
| Timeout:VacuumDelay（autovacuum） | cost-based delayによる抑制。追い付かない原因になり得る | [maintenance-replication.md](maintenance-replication.md) |

PostgreSQL 18はfast-pathロックのスロット数が`max_locks_per_transaction`に応じて増える。PostgreSQL 18の非同期I/O（`io_method`）では、I/O待ちがI/O workerや別の待機イベント名に現れる場合がある。待機イベント名は版で変わるため、実サーバの`pg_wait_events`ビュー（PostgreSQL 17以降）で説明を確認する。

## 注意

- サンプリングは点観測。間隔より短い待機は見えにくく、割合は時間の推定値。
- poolで待っているリクエストはDBに現れない。アプリ側のpool待ちと合わせて見る。
- 1つの待機が高いことは原因の候補であり、その待機を減らす設定変更を直ちに提案しない。上位のquery_idとその計画を確認する。
- `idle in transaction`はDB時間を使わないが、ロックとVACUUMの回収を妨げる。DB時間の内訳とは分けて報告する。

## 公式資料

- [待機イベント](https://www.postgresql.org/docs/18/monitoring-stats.html#WAIT-EVENT-TABLE)
- [pg_stat_activity](https://www.postgresql.org/docs/18/monitoring-stats.html#MONITORING-PG-STAT-ACTIVITY-VIEW)
- [pg_wait_events](https://www.postgresql.org/docs/18/view-pg-wait-events.html)
