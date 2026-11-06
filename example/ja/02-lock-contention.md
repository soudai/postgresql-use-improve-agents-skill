# 可視性が不足する状況でロック連鎖を調べる

[English](../en/02-lock-contention.md) · [ユースケース一覧](README.md)

架空の入力に基づくレポート例です。実環境の測定結果やコマンドの出力そのものではありません。
以下のファイル名は入力の例です。プロンプトを使う際は自身の環境のエクスポートを用意してください。
これらの入力ファイルは同梱していません。

## 状況と提供する根拠

PostgreSQL 18で注文の更新が断続的に詰まっています。症状が出ている間に、
設定上1秒間隔でactivityを60回サンプリングしました。採取側の`track_activities`はonですが、
別の一部セッションのstate/typeは非表示でした。

| 根拠 | 提供値 |
| --- | --- |
| 観測できたactiveの延べ数 | 60サンプルで180セッション観測 |
| 観測した待機 | `Lock:transactionid`が144、`No reported wait event`が36 |
| 判定不能の延べ数 | 120セッション観測。state/backend typeが非表示 |
| snapshotの根本blocker | PID 4101は`idle in transaction`。transaction ageは180秒、blocking PIDなし |
| 同じsnapshotの直接blocker | 4201 → [4101]、4202 → [4101]、4301 → [4201] |
| snapshotの識別情報 | backend startを記録済み。4つのPIDは別々の現在のセッション |
| 不足する根拠 | SQL本文、トランザクション内の過去のSQL、アプリtrace、OSのCPU指標 |

blockerの行の矢印は`pg_blocking_pids()`に対応する**待機者 → blocker**です。
blockerが保持するロックは、以前のSQLで取得された可能性があります。

## プロンプト

```text
$postgres-performance-diagnostics を使い、注文更新の断続的な詰まりを調査してください。
activityのサンプリング結果samples.jsonと、同じ時刻のactivity/lock snapshotおよび
pg_blocking_pids()の結果を含むincident.jsonを提供します。
入力の内容は上の根拠表のとおりです。

根本blocker、直接・間接の待機者、チェーンの深さを再構成してください。
非表示セッションがAASと待機割合の母数に与える影響を説明してください。
事実と仮説を分け、原因となるトランザクション境界を調べるための追加入力を示してください。
レポートと対象を絞った検証計画を作り、この依頼ではセッションのcancel/terminateは
行わないでください。
```

## 結果の例

**優先して調べるのはPID 4101が保持するトランザクションです。** このsnapshotでは、
間接待機を含む**3セッション**の根本blockerで、チェーンの最大深さは**2段**です。
以下の図は向きを逆にして、**blocker → 待機者**で表しています。

```text
4101 (idle in transaction, age 180 s)
├── 4201
│   └── 4301
└── 4202
```

アプリが待機している間もトランザクションが開いたままです。トランザクションの保持と
blockingは確認できますが、どの過去SQLが各ロックを取得したかは未確定です。
snapshotだけではdeadlockやテーブル設計の欠陥までは判断できません。

| 指標 | 解釈 |
| --- | --- |
| `activity_coverage` | 一部セッションのactivityが非表示のため`incomplete` |
| `average_active_sessions` / `peak_active_sessions` | 全体を評価できないため`null` |
| `observed_average_active_sessions` | 180 / 60 = **3.0**。サンプリング対象範囲の下限 |
| Lock待機の割合 | 144 / 180 = **観測できたactiveの延べ数の80%** |
| 残りの観測割合 | 36 / 180 = **20%**。待機イベントの報告なし。CPU飽和の証明ではない |

判定不能の120セッション観測を、idle・background・負荷0とは分類できません。
80%はサーバ全体の待機割合ではありません。PID 4101のidle時間はactiveの時間と分けて報告します。
DB時間を多く使わないセッションでもblockerになり得ます。

## 次の検証

1. 利用が許可された監視ロールで再採取し、可視性が完全になるかを確認します。
   採取頻度を限定し、正常時とも比較します。
2. backend start、リクエストtrace、トランザクション履歴を照合します。
   BEGINからCOMMITまでの処理、外部呼び出し、例外経路を特定します。
3. 対象フローでトランザクション短縮やcommit/rollback経路の修正を試します。
   ロール単位のtimeoutは、正当な長時間処理への影響も評価します。
4. 同等の負荷で待機時間、チェーンの深さ、トランザクション時間、API応答時間、再試行エラーを比較します。
   アプリやtimeout変更の切り戻しを記録します。

セッション終了は、対象リクエストとrollbackコストを踏まえた運用判断が別途必要です。
現在のSQLをcancelするだけでは、idleのトランザクションと保持ロックが残る場合があります。

[スキル](../../skills/postgres-performance-diagnostics/SKILL.md)、
[待機イベントの手順](../../skills/postgres-performance-diagnostics/references/wait-events.md)、
[ロック調査の手順](../../skills/postgres-performance-diagnostics/references/locking.md)、
[activityサンプラー](../../skills/postgres-performance-diagnostics/scripts/sample_activity.py)を参照してください。
