# 07. 負荷検証: 速く見えても結果が違う候補を採用しない

[English](../en/07-workload-validation.md) · [ユースケース一覧](README.md)

この例は、比較スクリプトの判定を確認するための架空の機能検証データです。Python 3だけでオフライン実行でき、DBには接続しません。実測のベンチマーク結果や、性能改善の証拠ではありません。

## 状況と入力

注文集計のSQL変更を比較する想定です。[baseline.json](../inputs/workload/baseline.json)と[candidate.json](../inputs/workload/candidate.json)には、同じ比較条件と、各1秒・成功4件の1試行を記録しています。数値はこの例のために作成した値です。

| 項目 | baseline | candidate |
| --- | --- | --- |
| 成功イベントの時間 | 12、14、16、18 ms | 6、7、8、9 ms |
| 結果の照合値 | `orders=4;total=100` | `orders=4;total=99` |
| DB・OS指標 | CPU、読み取りI/O、WAL、OOM | CPU、読み取りI/Oのみ |
| 独立した試行 | 1回 | 1回 |
| 検証計画（policy） | なし | なし |

`conditions`の`server`は`synthetic-no-database`、`cache`は`synthetic`です。実環境の版やキャッシュ状態を表していません。`complete: true`は、この架空のイベント配列が全4件を含むことを示します。

## プロンプト例

リポジトリのルートを作業ディレクトリにして、次を渡します。

```text
$postgres-workload-validation

example/inputs/workload/baseline.json と
example/inputs/workload/candidate.json を比較してください。
これは比較ロジックを確認するための架空のデータです。
DB接続や負荷の再生は不要です。

例の閾値は成功p95が20 ms以下、完了TPSが4以上、
失敗・タイムアウト・未送信の合計率が0、OOM kill数が0です。
既存の compare_runs.py を実行し、correctness、performance、
evidence_quality、improvement を分けて報告してください。
指標の欠測や試行不足があっても結果不一致を取り消さないでください。
性能改善や本番採用を主張せず、必要な追加検証を示してください。
```

## オフラインで再現する

リポジトリのルートで実行します。これらの閾値はこの機能検証用であり、実運用の推奨値ではありません。

```sh
python3 skills/postgres-workload-validation/scripts/compare_runs.py \
  example/inputs/workload/baseline.json \
  example/inputs/workload/candidate.json \
  --p95-ms 20 --min-tps 4 --max-loss-rate 0 --metric-max oom_kills=0
```

Python 3のコマンド名が`python`の環境では、`python3`を`python`に置き換えます。JSONは標準出力に出ます。`--policy`を省略しているため、採用判定に必要な検証計画は不足したままです。

## 結果例

実際のCLI出力から、以下のフィールドだけを抜粋しています。

```json
{
  "schema_version": 2,
  "decision": "rollback_candidate",
  "correctness": {
    "status": "mismatch",
    "observed_digest_difference": true,
    "conditions_comparable": true
  },
  "performance": {
    "status": "slo_met_in_observed_runs",
    "violations": []
  },
  "evidence_quality": {
    "status": "insufficient"
  },
  "improvement": {
    "status": "not_established"
  },
  "missing": [
    "2 or more runs required per variant",
    "validation policy missing: sample size, observation period, variability and representative workload",
    "candidate 1: missing DB or OS metrics: oom_kills, wal_bytes",
    "candidate 1: missing oom_kills"
  ],
  "regressions": [
    "result mismatch"
  ]
}
```

| 評価 | 読み方 |
| --- | --- |
| correctness | 条件が一致する入力で照合値が異なるため、結果不一致。 |
| performance | この配列から算出したcandidateのp95は9 ms、完了TPSは4、損失率は0。入力された閾値を満たす。 |
| evidence_quality | 各1試行、検証計画なし、candidateのWALとOOM指標が欠測のため証拠不足。 |
| improvement | 性能改善は未確立。baselineのp95は18 msだが、4件の架空データから改善は主張できない。 |

判定は`rollback_candidate`です。計測不足があっても、確認できた結果不一致は残ります。スクリプトはJSONを出力するだけで、SQLや設定の切り戻しを実行しません。1試行の最小・中央値・最大が同じでも、試行間の安定性を確認したことにはなりません。

## 次の対応

1. 結果不一致の原因を調べ、仕様で定めた期待結果と照合します。照合値だけを同じ文字列に書き換えて済ませないでください。
2. 実際のSQL、パラメータの分布、読み書き比率、初期データを使って代表負荷を定めます。検証先、書き込み範囲、停止条件を確定します。
3. 試行数、成功件数、観測時間、許容ばらつきを根拠付きの検証計画に定め、`--policy`で指定します。入力可能な最小値は十分性を保証しません。
4. DB・OS・負荷生成側の指標を同じ観測窓で収集し、欠測を0で埋めず、独立した試行で再検証します。他SQLへの影響と変更以外の条件差も調べます。

手順と入力仕様: [postgres-workload-validation](../../skills/postgres-workload-validation/SKILL.md) · [負荷の設計と実行](../../skills/postgres-workload-validation/references/replay.md) · [比較スクリプト](../../skills/postgres-workload-validation/scripts/compare_runs.py)
