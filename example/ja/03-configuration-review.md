# 根拠を確認してcheckpointとJITの設定をレビューする

[English](../en/03-configuration-review.md) · [ユースケース一覧](README.md)

架空の入力に基づくレポート例です。実環境の測定結果やコマンドの出力そのものではありません。
以下のファイル名は入力の例です。プロンプトを使う際は自身の環境のエクスポートを用意してください。
これらの入力ファイルは同梱していません。

## 状況と提供する根拠

PostgreSQL 18でトランザクション処理とレポート処理を運用しており、応答時間が変動しています。
WALの設定を大きくするべきか、JITを無効にするべきかをレビューします。

| 根拠 | 提供値 |
| --- | --- |
| 利用可能な資源 | RAM 64 GiB、16 vCPU、SSD。実際の同時実行クエリ数は未測定 |
| 設定 | `max_wal_size=1GB`、`checkpoint_timeout=300s`、`jit=on` |
| workerの上限 | `max_parallel_workers=32`、`max_worker_processes=64`。実際の稼働数は不明 |
| 統計の観測窓 | 1,800秒。同じinstance・起動時刻で、必要なカウンタは揃い、resetはない |
| checkpoint差分 | `num_requested=9`、`num_timed=1` |
| 運用履歴 | 手動CHECKPOINTが3回。checkpointの原因ログは未取得 |
| top-level SQL差分 | 観測対象entryの`total_exec_time=100,000 ms` |
| 同じentryのJIT差分 | generation 4,000 ms、inlining 1,000 ms、optimization 2,000 ms、emission 1,000 ms |
| JITの内数 | deform 3,000 ms。generationに含まれる |
| 不足する根拠 | WAL/FPI量、WAL保持量、ディスク余裕、復旧目標、SQL別のJITの影響 |

設定はアプリと同じロール・DBで取得しました。sourceとoverrideの情報を含み、
再起動待ちの設定は報告されていません。

## プロンプト

```text
$postgres-config-tuning を使い、checkpointとJITの設定をレビューしてください。
settings.json、性能の区間レポートinterval.json、同じ1,800秒間の運用記録を提供します。
入力の内容は上の根拠表のとおりです。

根拠のある変更と追加調査が必要な項目を説明してください。checkpointの原因と
JIT時間の内訳を確認し、workerの設定上限と実際の同時実行を区別してください。
根拠の足りない推奨値はnullのままにしてください。
現在値、優先度、根拠、適用範囲、リスク、検証・切り戻し計画を示してください。
この依頼ではレビューまでとし、設定は適用しないでください。
```

## 結果の例

**checkpointの原因調査と、対象を絞ったJIT比較を優先します。** 提供されたカウンタだけでは、
WAL設定の増加やJITの無効化が有利とは判断できません。

| 項目 | 現在値 | 推奨値 | 根拠と次の対応 |
| --- | --- | --- | --- |
| `max_wal_size` | 1GB | 未定（`null`） | 要求checkpointは9 / (9 + 1) = 90%。ログで原因を照合する |
| `jit` | on | 未定（`null`） | top-level SQL実行時間の8%がコンパイル。代表SQLでon/offを比較する |
| parallel worker | 上限32、process上限64 | 未定（`null`） | 上限が16 vCPUを超えるだけでは、同時実行のCPU競合は確認できない |
| メモリ配分 | 実際の同時実行数は不明 | 未評価 | 同時実行負荷とメモリを使う計画ノードを確認してから試算する |

1,800秒の観測窓は`checkpoint_timeout`の3倍を超えますが、十分な長さだけで原因は分かりません。
手動操作で説明できる要求は3回で、残りの原因は未確定です。
要求checkpointの数だけではWAL圧力の証明になりません。
`max_wal_size`はcheckpointに関するソフト上限であり、ディスク使用量の上限ではありません。

JITコンパイル時間は**4,000 + 1,000 + 2,000 + 1,000 = 8,000 ms**で、
100,000 msの**8%**です。deformを再加算すると誤って11%になります。
レビューの5%という目安は調査の入口であり、純粋な損失の測定値ではありません。
レポートSQLでは、JITによる実行の短縮がコンパイル時間を上回る可能性もあります。

## 次の検証

1. 同じ区間のcheckpoint原因ログと運用履歴を照合します。WAL/FPIの発生速度、保持量、
   ディスク余裕、復旧要件を取得してから設定値を検討します。
2. 代表的なトランザクション処理とレポートSQLを、ステージングの限定したセッションでon/off比較します。
   データ、パラメータ、prepared planの扱い、キャッシュ、同時負荷を揃え、複数回測定します。
   総時間にはコンパイル時間も含めます。
3. 総応答時間、ばらつき、処理量、レポートSQLへの影響を記録します。
   合意した目標を改善し、重大な退行がない場合にロール・セッション単位の変更を採用します。
4. worker制限の変更前に、実稼働数、OSのCPUとrun queueを観測します。
   メモリ需要は別途測定し、接続上限から実際の同時実行を推測しません。

セッション単位のJIT比較は、記録した設定値への復元または検証セッションの終了で戻します。
その後に永続的な変更を行う場合は、元の値・source・ロール/DBのoverrideを記録したうえで、
適用と切り戻しの計画を別途用意します。この例では前後の改善効果は未測定です。

[スキル](../../skills/postgres-config-tuning/SKILL.md)、
[パラメータの手順](../../skills/postgres-config-tuning/references/parameters.md)、
[設定レビュースクリプト](../../skills/postgres-config-tuning/scripts/review_config.py)、
[適用・検証手順](../../skills/postgres-config-tuning/references/apply-and-verify.md)を参照してください。
