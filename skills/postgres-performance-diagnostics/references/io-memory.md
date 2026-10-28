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

PostgreSQLのbuffer missはOSページキャッシュで満たされることがあり、物理ディスクI/Oと同じではない。高ヒット率でも論理読み取りが大量ならCPUやメモリ帯域を消費する。低ヒット率の一括処理を無条件に異常にしない。

`pg_stat_io`はbackend_type／object／context等の集計。単独で特定SQLやテーブルに帰属させず、SQL別統計、relation統計、計画と観測時間を照合する。SQLとテーブルで見えるアクセスを足してクラスタの物理I/Oにしない。版による列の単位の差、block_size、read／write／extend／fsyncの区別を確認する。

I/O時間の収集設定が無効だった区間では、時間0をI/Oなしとしない。デバイスのIOPS、転送量、遅延、キュー、帯域／burst制限を同時刻で確認する。cold／warm cache、再起動、データ増加を前後比較の条件に含める。

改善候補は走査削減、索引、アクセス局所性、working set、ストレージ能力を比較する。`shared_buffers`をヒット率だけで決めず、`effective_cache_size`はメモリを確保する設定ではないことを説明する。

## temp spill

SQL別tempブロック、DB別temp量、`log_temp_files`、JSON計画を使い、総量／実行・毎秒の量とI/O時間を見る。`pg_stat_database`のtemp合計だけではSQLを特定できない。ログのsession、PID、query ID、時刻、parallel workerを照合する。

Sortのdisk使用、Hashの複数batch、集約やJOINの中間結果を調べる。表への帰属はSQLとplan nodeで確認する。spillには許容範囲があり、発生しただけでOOMより悪いとは扱わない。

対策は早い絞り込み、必要列削減、索引、集約方法、限定した処理へのメモリ調整を比較する。`temp_buffers`は一時テーブルのバッファであり、sort／hash spillの予算ではない。

`work_mem`は接続全体の固定使用量ではない。複数演算・同時クエリ・parallel workerで使用が重なり、hashでは`hash_mem_multiplier`も関係する。概算は計画上で同時に動く演算から作り、単純な接続数×work_memを実使用量と断定しない。OS余力、maintenance系処理、OOM／swap、tempディスク容量を含めて予算を評価する。

## 接続とCPU・メモリ

DB内実行、lock／I/O待ち、pool待ち、接続確立、ネットワーク、結果受信・変換を分ける。pool待ちのリクエストはDBのactivityにまだ現れない場合がある。

CPU・I/O・LWLockのどこに時間を使っているかは、まず[待機のサンプリング](wait-events.md)で内訳を取る。CPUの割合が高ければ、`analyze.py`の`sql_shared_blks_accessed`（ヒットを含む論理読み取り）で上位のSQLを探す。ヒット率が高くても論理読み取りが多ければCPUを消費する。

`active`かつwait_eventがNULLというだけでCPU稼働を確定しない。SQL実行時間はCPU時間ではない。OSのプロセス別CPU、RSS、swap、OOM、cgroup／クラウド制限と同時刻で突き合わせる。

接続数と生成速度、active率、poolの上限・待ち時間、長いトランザクション、parallel workers planned/launched、worker上限を確認する。同時実行数の制御、pool調整、処理時間帯の分散、SQL改善、資源増強を比較し、`max_connections`・メモリ・parallel workerの一律増量を既定策にしない。

## 検証

- 低ヒット率だが許容できる一括処理と、遅延の大きいI/O待ちを区別する。
- Sort spill／Hash spillを再現し、原因演算とSQLを関連付ける。
- CPU飽和、pool待ち、I/O待ち、メモリ圧迫、worker不足を別々に報告する。
- 同じ負荷とcache条件で、API応答時間、throughput、pool待ち、CPU、メモリ、I/O、temp、OOMの前後を確認する。

## 公式資料

- [累積統計・pg_stat_io](https://www.postgresql.org/docs/18/monitoring-stats.html)
- [リソース設定](https://www.postgresql.org/docs/18/runtime-config-resource.html)
- [監視とOS指標](https://www.postgresql.org/docs/18/monitoring.html)
- [Parallel Query](https://www.postgresql.org/docs/18/parallel-query.html)
