# I/O・temp・メモリ・接続の診断

## ヒット率と読み取り量

同じ区間の`pg_stat_io`、`pg_statio_*`、SQL統計、relationサイズ、OS／クラウド指標を使う。ヒット率は母数と絶対量を併記する。

```text
SQL単位のshared buffer hit ratio
  = Δshared_blks_hit / (Δshared_blks_hit + Δshared_blks_read)

テーブルheapのhit ratio
  = Δheap_blks_hit / (Δheap_blks_hit + Δheap_blks_read)
```

前者は`pg_stat_statements`、後者は`pg_statio_user_tables`の同じ単位のブロック数から求める。分母0は未定義。`pg_stat_io.reads`は複数ブロックをまとめたI/O操作の回数を数える場合があるため、一般形の`Δhits / (Δhits + Δreads)`をどのビューにもそのまま適用しない。実バージョンの列定義と単位を確認する。

区間レポートの`sql_shared_read_bytes`、`sql_shared_read_ms`、`table_heap_reads`、`index_reads`を照合する。ブロック数と時間の両方で上位を確認し、区間秒数・ヒット率の母数・絶対量を読む。SQL単位では実行当たりの量と時間も確認する。`sql_shared_blks_accessed`はヒットが多くても大量にバッファへアクセスするSQLを見つけるのに使う。

`read_time_share`は累積読み取り時間を累積実行時間で割った目安。並列処理で待ちが重なると1を超え得る。低ヒット率でも値が小さくSLOを満たす一括処理は、改善優先度を下げられる。読み取り時間が大きく応答時間も悪化していれば、同じ時刻の待機サンプル（`IO:DataFileRead`等）とストレージ遅延で仮説を確認する。OS指標がない場合は、DBで観測した読み取り量と待ち時間までを報告する。SQLの対象relationは実測計画のノードで確認する。

`compare_intervals.py`は共有読み取りバイト数・時間と実行時間を実行当たりで比較する。ヒット率の差はキャッシュ条件を確認するきっかけにし、計画や走査範囲の変化も調べる。事前に同じデータ、パラメータ分布、同時実行数、warm-up手順を揃え、shared buffersとOSキャッシュの状態を区別して記録する。

PostgreSQLのbuffer missはOSページキャッシュで満たされることがあり、物理ディスクI/Oと同じではない。高ヒット率でも論理読み取りが大量ならCPUやメモリ帯域を消費する。低ヒット率の一括処理を無条件に異常にしない。

`pg_stat_io`はbackend_type／object／context等の集計。単独で特定SQLやテーブルに帰属させず、SQL別統計、relation統計、計画と観測時間を照合する。SQLとテーブルで見えるアクセスを足してクラスタの物理I/Oにしない。収集したビューの列を`capabilities`で確認する。18と19 RC1の`read_bytes`・`write_bytes`はバイト、`read_time`・`write_time`はミリ秒、`reads`・`writes`は操作回数。読み書き量は対応するbyte列が取得できる場合に報告する。`pg_statio_*`と`pg_stat_statements`のブロック数をバイト換算するときは、収集した`block_size`を使う。

I/O時間の収集設定が無効だった区間では、時間0をI/Oなしとしない。デバイスのIOPS、転送量、遅延、キュー、帯域／burst制限を同時刻で確認する。cold／warm cache、再起動、データ増加を前後比較の条件に含める。

改善候補は走査削減、索引、アクセス局所性、working set、ストレージ能力を比較する。`shared_buffers`をヒット率だけで決めず、`effective_cache_size`はメモリを確保する設定ではないことを説明する。

## temp spill

SQL別tempブロック、DB別temp量、`log_temp_files`、JSON計画を使い、総量／実行・毎秒の量とI/O時間を見る。`pg_stat_database`のtemp合計だけではSQLを特定できない。ログのsession、PID、query ID、時刻、parallel workerを照合する。

Sortのdisk使用、Hashの複数batch、集約やJOINの中間結果を調べる。表への帰属はSQLとplan nodeで確認する。spillには許容範囲があり、発生しただけでOOMより悪いとは扱わない。

対策は早い絞り込み、必要列削減、索引、集約方法、限定した処理へのメモリ調整を比較する。`temp_buffers`は一時テーブルのバッファであり、sort／hash spillの予算ではない。

`work_mem`は接続全体の固定使用量ではない。複数演算・同時クエリ・parallel workerで使用が重なり、hashでは`hash_mem_multiplier`も関係する。概算は計画上で同時に動く演算から作り、単純な接続数×work_memを実使用量と断定しない。OS余力、maintenance系処理、OOM／swap、tempディスク容量を含めて予算を評価する。

## 接続とCPU・メモリ

DB内実行、lock／I/O待ち、pool待ち、接続確立、ネットワーク、結果受信・変換を分ける。pool待ちのリクエストはDBのactivityにまだ現れない場合がある。traceに`pool_wait_ms`を記録すれば、`analyze_trace.py`がエンドポイントごとのpool待ちの割合を出す。

| 観測できる場所 | 分かること |
| --- | --- |
| DB統計（待機サンプル、`active_time`、`pg_stat_statements`） | DB内の実行・待機、平均アクティブセッション、SQL別の実行時間、parallel workerの計画数と起動数 |
| OS／コンテナ（CPU使用率、run queue、RSS、swap、cgroupの`memory.events`） | CPU飽和の確定、メモリ圧迫、OOM killの発生 |
| アプリ（trace、poolの指標） | pool待ち、接続確立、ネットワーク、結果変換、利用者の応答時間 |

CPU飽和は、平均アクティブセッション数がCPU数を超え、待機イベントのない実行が大半を占め、OSのCPU使用率も上限に張り付く状態。同時実行をpoolで絞ると、DB内の1クエリ当たり時間は下がり、待ちはアプリ側のpool待ちに移る。throughputと利用者の応答時間で評価する。OOM killは`analyze_logs.py`の`backend_killed`（signal 9）とcgroupの`oom_kill`で確認する。全セッションが再接続になるため、work_mem×同時実行の予算を見直す。

CPU・I/O・LWLockのどこに時間を使っているかは、まず[待機のサンプリング](wait-events.md)で内訳を取る。CPUの割合が高ければ、`analyze.py`の`sql_shared_blks_accessed`（ヒットを含む論理読み取り）で上位のSQLを探す。ヒット率が高くても論理読み取りが多ければCPUを消費する。

`active`かつwait_eventがNULLというだけでCPU稼働を確定しない。SQL実行時間はCPU時間ではない。OSのプロセス別CPU、RSS、swap、OOM、cgroup／クラウド制限と同時刻で突き合わせる。

接続数と生成速度、active率、poolの上限・待ち時間、長いトランザクション、parallel workers planned/launched、worker上限を確認する。同時実行数の制御、pool調整、処理時間帯の分散、SQL改善、資源増強を比較し、`max_connections`・メモリ・parallel workerの一律増量を既定策にしない。

同じ時間帯の接続数と同時実行数を分けて記録する。`pg_stat_database.sessions`の区間差分はDBに確立した接続数で、`sessions/s`が急増したらpool再利用率や切断・接続エラーを調べる。接続上限と比較する総数はクラスタ単位で採る。`sample_activity.py`のactive数にはparallel workerも含まれる。接続数にはクラスタ全体のclient backendを数える。

| 症状 | 確認する証拠 | 比較する対策 |
| --- | --- | --- |
| CPU飽和 | activeで待機イベントなし、プロセスCPU、CPU quota・throttling、run queue | 同時実行を制限し、DB時間が下がる幅とpool待ち・API p95の増減を比較 |
| I/O待ち | `IO:*`の時系列、SQLのread ms、デバイス遅延・キュー・読み取りバイト数 | 走査・結果量を削減し、同じ負荷・キャッシュ条件で読み取り量と応答時間を比較 |
| pool待ち・接続確立 | poolの取得待ち、空き・使用中・待ちリクエスト数、接続確立時間 | poolの再利用・上限、接続の急増抑制、重い処理の時間帯分散を比較 |
| 結果受信が遅い | `ClientWrite`のサンプル、受信バイト数、first rowから最終行受信、変換時間 | 取得列・行数を減らし、転送量と結果の正しさを確認 |
| メモリ圧迫 | cgroup上限・メモリ使用量・OOM増分、swap、RSS、temp、同時に動くsort/hashと保守処理 | 演算・worker・同時クエリ単位の予算を見直し、OOM、spill、実行時間を比較 |
| parallel worker不足 | 実測計画のplanned／launched、SQL・DB別の区間差分、同時実行とworker枠 | 同時実行や並列度を調整し、SQL時間と全体throughputを比較 |

アプリ側では、リクエスト開始、pool取得開始・完了、新規接続確立、SQL送信、最初の行と最終行の受信、変換終了を同じtraceに記録する。`analyze_trace.py`でAPI p95、pool待ちp95、計測リクエスト数を比較する。DB側は同じ時間帯のSQL統計と待機サンプルを使い、クライアント計測のDB呼び出し時間には通信と結果受信も含まれ得ることを踏まえる。CPU、メモリ、ストレージはcgroup・OS・クラウドから別途採り、欠測している区間は結論の範囲を明示する。

## 検証

- 低ヒット率だが許容できる一括処理と、遅延の大きいI/O待ちを区別する。
- Sort spill／Hash spillを再現し、原因演算とSQLを関連付ける。
- CPU飽和、pool待ち、I/O待ち、メモリ圧迫、worker不足を別々に報告する。
- 同じ負荷とcache条件で、API応答時間、throughput、pool待ち、CPU、メモリ、I/O、temp、OOMの前後を確認する。

## 公式資料

- [累積統計・pg_stat_io](https://www.postgresql.org/docs/18/monitoring-stats.html)
- [PostgreSQL 19の累積統計](https://www.postgresql.org/docs/19/monitoring-stats.html)
- [リソース設定](https://www.postgresql.org/docs/18/runtime-config-resource.html)
- [監視とOS指標](https://www.postgresql.org/docs/18/monitoring.html)
- [Parallel Query](https://www.postgresql.org/docs/18/parallel-query.html)
