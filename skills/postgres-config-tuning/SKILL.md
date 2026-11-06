---
name: postgres-config-tuning
description: Review and optimize PostgreSQL 18 and 19 server configuration (postgresql.conf, ALTER SYSTEM, role/database settings, managed-service parameter groups). Use when asked to tune, size or audit memory, connections, WAL/checkpoints, autovacuum, planner, I/O, parallelism, JIT, logging or timeouts; produce evidence-backed changes with apply, verify and rollback steps.
---

# PostgreSQLの設定最適化

ハードウェア・ワークロード・観測した根拠に合わせて設定の変更案を作る。万能の推奨値を当てはめない。変更は案として示し、許可された範囲でだけ適用する。

## 進め方

1. **前提を確認する**: PostgreSQLが使えるメモリとvCPU、ストレージ種別（ローカルSSD／ネットワーク／HDD）、自前運用かmanaged serviceか、ワークロード（OLTP／分析／混在）、pool後の同時実行数、データ量、レプリケーション構成、許容できるデータ損失と復旧時間。分からない値は推測で埋めず、その項目を評価対象外として示す。
2. **現在の設定を取得する**: スキルフォルダから`psql -X -qAt --set=instance_id=prod-primary --file sql/settings.sql > settings.json`。全`pg_settings`を、資格情報・コマンド・パスを伏せてJSONで出す。`source`で既定値か設定済みかを、`pending_restart`で再起動待ちの変更を確認する。アプリと同じ接続ロール・DBで取得し、適用元とoverrideを確認する。
3. **根拠を集める**: 設定変更の必要性は症状から判断する。`postgres-performance-diagnostics`スキルの区間レポート（`analyze.py --format json`）があれば使う。checkpointの要求数と原因ログ、WAL量とFPI、temp量、JIT時間、平均アクティブセッション数を読む。手動CHECKPOINTも要求数に含まれるため、その比率だけでWAL不足と判断しない。
4. **レビューする**:

   ```text
   python scripts/review_config.py settings.json --ram-gb 64 --cpus 16 --storage ssd \
     --workload oltp --max-active-connections 80 --interval-report interval.json --output review.md
   ```

   出力は条件付きの調査・変更候補であり、数値の閾値は最適値や原因の証明ではない。並列workerの設定上限と実稼働数も区別する。[parameters.md](references/parameters.md)で各パラメータの考え方と例外を確認し、環境に合わせて値を決める。
5. **変更計画を作り、検証する**: [apply-and-verify.md](references/apply-and-verify.md)に従い、反映方法（reload／restart／ロール単位）、順序、確認指標、切り戻しを示す。

## 判断の原則

- 症状と根拠に結び付く変更を優先する。根拠のない変更を一度に多数行わない。複数変更すると効果と退行の原因を切り分けられない。
- データの安全性（`fsync`、`full_page_writes`、`synchronous_commit`）を性能のために緩める案は既定にしない。
- `work_mem`等のセッション単位の値は、全体の既定値より、必要なロール・処理に限定して上げることを先に検討する。
- managed serviceでは変更できない・名前が違う・独自の計算式を使うパラメータがある。提供者のparameter groupの仕様に従う。
- メモリの配分は、`shared_buffers`、同時実行の`work_mem`、maintenance系、OSのページキャッシュの合計で考える。単一の値だけを最適化しない。
- JITコンパイル時間の比率だけで無効化しない。同じ代表処理をON/OFFで複数回比較し、コンパイルを含む総時間と他処理への影響を確認する。
- SQLや索引で解決できる問題（大量走査、spill）を、設定値の増加で覆い隠さない。

## 成果物

最初に優先度の高い変更（安全性、明らかな誤設定、根拠のある性能改善）を示す。追加証拠が必要な候補は推奨値を未定（JSONではnull）として次の調査を示す。各変更に、現在値と推奨値、理由と根拠、反映に必要な操作（reload／restart）、リスク、確認する指標、切り戻し手順を添える。評価できなかった項目と、追加で必要な情報も示す。適用用のSQL・設定ファイルの差分は、レビューのための草案として示す。

代表負荷の再生とSLOの判定には`postgres-workload-validation`を使う。
