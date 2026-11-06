# 負荷の設計と実行

- データの量と偏り、代表パラメータの分布、読み書きの構成比、DB・拡張・設定、クライアント版を記録する。更新系は各runで同じ初期データを使い、件数やチェックサムで結果を検証する。
- 検証先と書き込み範囲を確定する。負荷生成側とDB側のCPU・メモリ・I/Oを別々に観測し、エラー率や待ちの停止条件を決める。warm-upと計測を別runにする。
- 固定クライアント数で飽和時の処理量を確認する。目標到着率でSLOを見るときはpgbenchの`--rate`と`--latency-limit`を使い、遅延・未送信・失敗を記録する。接続方式と試行順も固定または記録する。
- 各runのUTC時刻にDB統計・待機、OS／cgroup、APMのpool待ちと応答時間を合わせる。時計の差、取得範囲とサンプリング率を記録する。DBベンチの時間とAPIの時間は別の系列として評価する。

検証用DBに対する例。`read.sql`と`write.sql`に代表パラメータの分布を組み込む。読み取りだけの試験では読み取り用スクリプトを指定する。

```sh
pgbench -n -M prepared -c 8 -j 2 -T 60 -R 200 -L 100 -l \
  --log-prefix=baseline-01 -f read.sql@9 -f write.sql@1 'service=bench'
```

ログは非集約・非サンプリングを使い、全workerファイルを保存する。集約ログしかない場合は成功分位点を欠測として記録する。個別のAPMイベントから比較する場合は、同じエンドポイント・サンプリング条件の全イベントを入力する。実行の結果とログ件数を照合してから`complete`をtrueにする。

## 比較入力

baselineとcandidateは同じ形式。`conditions`は変更差分を除いた比較条件で、実行先の版、データとスクリプトの識別子、分布、クライアント数、到着率、キャッシュ条件を記録する。pgbenchの`--rate`を使った場合は`rate`、`--max-tries`を変えた場合は`max_tries`を指定する。`metrics`は同じ窓・単位の時系列集計。例のキーは比較コマンドの`--metric-max`で選べる。計測できない場合はキーを省く。`complete`はログの範囲と実行結果が確認できた場合に付ける。

```json
{
  "conditions": {"data": "fixture-v1", "mix": "read90-write10", "distribution": "zipf-v1", "clients": 8, "rate": 200, "cache": "warm", "server": "18.6"},
  "runs": [{
    "started_at": "2026-11-06T01:00:00Z", "elapsed_seconds": 60,
    "complete": true, "result_digest": "same-expected-result",
    "pgbench_logs": ["baseline-01.1234", "baseline-01.1234.1"],
    "metrics": {"cpu_pct": 70, "io_read_bytes": 65536, "wal_bytes": 8192, "oom_kills": 0}
  }]
}
```

`pgbench_logs`の代わりに正規化イベントの`events`を使える。イベントは`{"status":"ok","latency_ms":8}`、`{"status":"failed"}`、`{"status":"timeout"}`、`{"status":"skipped"}`の形式。pgbenchの到着率指定時は予定時刻からの時間に待ちが含まれる。成功p95/p99と失敗・未送信率を別に読む。

```sh
python scripts/compare_runs.py baseline.json candidate.json \
  --p95-ms 100 --min-tps 180 --max-loss-rate 0.01 --metric-max oom_kills=0
```

2回以上の各試行を入力する。結果不一致、SLO超過、設定した資源上限の超過は切り戻し候補となる。欠測や条件差は再調査とし、最小・最大と中央値を確認する。CPU・I/O、pool、負荷生成側の飽和や他SQLの退行は同じ時間窓で確認する。

[pgbench公式資料](https://www.postgresql.org/docs/18/pgbench.html)

負荷生成側のCPU上限を判定する場合は各試行の`metrics.load_generator_cpu_pct`を記録し、`--load-generator-cpu-max`を指定する。`median_change`でSLO内の悪化も確認する。
