---
name: postgres-workload-validation
description: Compare PostgreSQL SQL, index, or configuration changes under a reproducible workload. Use for benchmark design, pgbench replay, capacity and latency regressions, and accept, investigate, or roll back decisions.
---

# PostgreSQLの負荷検証

対象処理とSLO、変更差分、平常時と繁忙時の代表負荷を確認する。実行先、データ準備、書き込み範囲、停止条件を確定してから再生する。取得済みのログと計測結果があれば先に使う。

[負荷の設計と実行](references/replay.md)に沿って入力と測定条件を揃え、同じ負荷を複数回再生する。比較には`scripts/compare_runs.py`を使う。DB統計・計画は`postgres-performance-diagnostics`、設定の反映確認と切り戻しは`postgres-config-tuning`の手順を使う。

結果一致とSLOを先に確認し、次に完了数、失敗・未送信、待ち時間、資源使用量を比較する。観測窓、各試行の値とばらつき、欠測、判断、切り戻し条件を報告する。
