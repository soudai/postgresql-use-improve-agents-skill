# 負荷の設計と実行

- データの量と偏り、代表パラメータの分布、読み書きの構成比、DB・拡張・設定、クライアント版を記録する。更新系は各runで同じ初期データを使い、件数やチェックサムで結果を検証する。
- 検証先と書き込み範囲を確定する。負荷生成側とDB側のCPU・メモリ・I/Oを別々に観測し、エラー率や待ちの停止条件を決める。warm-upと計測を別runにする。
- 固定クライアント数で飽和時の処理量を確認する。目標到着率でSLOを見るときはpgbenchの`--rate`と`--latency-limit`を使い、遅延・未送信・失敗を記録する。接続方式を揃え、試行順による時間変化を抑えるため交互実行・順序の無作為化を検討し、その方法を記録する。
- 各runのUTC時刻にDB統計・待機、OS／cgroup、APMのpool待ちと応答時間を合わせる。時計の差、取得範囲とサンプリング率を記録する。DBベンチの時間とAPIの時間は別の系列として評価する。

検証用DBに対する例。`read.sql`と`write.sql`に代表パラメータの分布を組み込む。読み取りだけの試験では読み取り用スクリプトを指定する。

```sh
pgbench -n -M prepared -c 8 -j 2 -T 60 -R 200 -L 100 -l \
  --log-prefix=baseline-01 -f read.sql@9 -f write.sql@1 'service=bench'
```

ログは非集約・非サンプリングを使い、全workerファイルを保存する。集約ログしかない場合は成功分位点を欠測として記録する。個別のAPMイベントから比較する場合は、同じエンドポイント・サンプリング条件の全イベントを入力する。実行の結果とログ件数を照合してから`complete`をtrueにする。[pgbenchのログと負荷生成仕様](https://www.postgresql.org/docs/18/pgbench.html)

## 比較入力

baselineとcandidateは同じ形式。以下は1試行の構造例であり、実際のrunsには計画した回数の独立した試行を入れる。`conditions`は変更差分を除いた比較条件で、実行先の版、データとスクリプトの識別子、分布、クライアント数、到着率、キャッシュ条件を記録する。pgbenchの`--rate`を使った場合は`rate`、`--max-tries`を変えた場合は`max_tries`を指定する。`metrics`は同じ窓・単位の時系列集計。例のキーは比較コマンドの`--metric-max`で選べる。比較は全試行のcpu_pct、io_read_bytes、wal_bytes、oom_killsを必須の観測として扱い、欠測なら証拠不足とする。計測できない値は0で埋めずキーを省く。`complete`はログの範囲と実行結果が確認できた場合に付ける。

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

## 検証計画

`--policy`には次の7キーだけを持つJSON objectを渡す。全キー必須で、追加・誤記したキーや型違いは入力エラーになる。SLOと資源上限は引き続きコマンド引数で指定する。

| キー | 型・意味 |
| --- | --- |
| schema_version | 整数1 |
| source | 空白だけではない文字列。代表性と閾値を決めた検証計画の参照先 |
| representative_workload | boolean。計画の対象負荷を代表すると確認した場合にtrue |
| min_runs | 2以上の整数。baseline/candidateそれぞれの最小試行数 |
| min_successful_events_per_run | 1以上の整数。各試行の最小成功件数 |
| min_elapsed_seconds | 0より大きい有限数。各試行の最小観測秒数 |
| max_relative_spread | 0以上の有限数。各variantのp95_msとcompleted_tpsに許す試行間の(max−min)/median |

`policy.json`の形式例:

```json
{
  "schema_version": 1,
  "source": "validation-plan/orders-2026-11-06",
  "representative_workload": true,
  "min_runs": 5,
  "min_successful_events_per_run": 10000,
  "min_elapsed_seconds": 60,
  "max_relative_spread": 0.10
}
```

この回数・件数・時間・ばらつきは例であり、十分性を保証する共通基準ではない。業務の分布、SLOの尾部、変動周期、検出したい差に合わせて事前に決める。入力可能な最小値を満たすだけで十分とはしない。sourceの資料とrepresentative_workloadの申告は自動検証されないため、実際のSQL・パラメータ・業務割合と照合する。中央値が0のばらつきは全値同一なら0、そうでなければ不明として扱う。

```sh
python scripts/compare_runs.py baseline.json candidate.json \
  --policy policy.json --p95-ms 100 --min-tps 180 \
  --max-loss-rate 0.01 --metric-max oom_kills=0
```

## 判定の読み方

出力はschema_version:2。従来のdecision、missing、regressions、各試行、median_change、min_median_maxに加え、評価を次のように分ける。

| 項目 | 意味 |
| --- | --- |
| correctness | 比較可能な条件での結果digestの一致・不一致・不明。観測した差と条件の一致を別フィールドにも残す |
| performance | candidateで観測したSLO違反、観測した試行でのSLO充足、または不明。資源上限の違反はregressionsに残る |
| evidence_quality | 計画、代表性、試行数、成功件数、時間、p95/TPSのばらつき、ログ・資源計測等の充足。十分とは入力された計画に対してのみの判定 |
| improvement | not_established。SLO充足と中央値差から因果的な改善を自動確定しない |

比較条件が一致する結果不一致、観測したSLO超過、設定した資源上限超過があればrollback_candidate。別の指標が欠測でもその違反は消さない。違反はなくてもpolicyなし、代表性未確認、計画未達、欠測、条件差があればinvestigateとする。違反と不足情報がない場合に限りaccept_candidateとなる。これは宣言した計画に基づく採用候補であり、全体の健全性や性能向上の証明ではない。

各試行の最小・中央値・最大を読み、median_change（candidate中央値−baseline中央値）から観測した差を示す。実装は統計的検定や因果推論を行わない。改善を主張するには、交絡する条件、試行順、他SQLへの影響を調べ、観測した差が変更に起因するかを別途検証する。

CPU・I/O、pool、負荷生成側の飽和や他SQLの退行は同じ時間窓で確認する。負荷生成側のCPU上限を判定する場合は各試行の`metrics.load_generator_cpu_pct`を記録し、`--load-generator-cpu-max`を指定する。この閾値の超過は生成側の飽和として証拠不足になる。DBベンチとAPIの待ち時間・エラーを同一指標として扱わない。
