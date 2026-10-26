# ロック・ロングトランザクション・deadlock

## 現在の待機を捕捉する

activity、locks、prepared transactionsのsnapshotを使う。発生中なら取得時刻を揃えた複数回の観測で追い、解消済みならログを優先する。PIDの再利用を避けるためbackend_start／session IDも残す。

`pg_blocking_pids()`を起点に待機元からblockerを辿り、根本blocker、待機チェーン、影響セッション数、対象DB／relation、lock modeを示す。単純な`pg_locks`自己JOINだけでblockerを確定しない。多数のセッションへ高頻度で繰り返し呼ぶと観測負荷があるため、必要な頻度に絞る。

| 観測 | 解釈 |
| --- | --- |
| xact_startが古い | トランザクションの長さ。現在のSQL時間とは別 |
| query_startが古い | そのSQLの開始。idle状態では直前SQLの場合がある |
| state_changeが古いidle in transaction | トランザクションを保持したままアプリが待っている可能性 |
| waitstart | その待機の開始。snapshotやロック種別によって取得できない場合がある |
| transactionid待ち | 行ロックに由来する場合がある。relationロックだけを探さない |
| blocking PID=0 | prepared transactionを確認する。通常backendへのcancel対象としない |

並列workerのleader、DDL、advisory lock、待機列の先行者も考慮する。activityに「現在のSQL」が見えても、そのSQLが保持中の全ロックを取得したとは限らない。トランザクションの過去の更新順序をアプリtraceと照合する。

ロック待ちを作っていない古いsnapshotもVACUUMを妨げ得る。[保守の手順](maintenance-replication.md)へ進む。

## deadlockと過去の待機

deadlock件数は傾向であり、原因となるSQLの一覧ではない。SQLSTATE、DETAIL、SQL、PID／session、時刻をログから再構成する。JSONログは`analyze_logs.py`でイベントを抽出し、必要に応じて本文を限定して読む。

`log_lock_waits`は`deadlock_timeout`を超えた待機を記録する。ログがなければ待機がなかった、という判断はしない。`deadlock_timeout`は検出とログの条件にも関わり、単なるクエリ実行上限ではない。

## 対処と再発防止

根本blockerの業務上の役割、トランザクションの更新量、ロールバック時間を確認する。必要に応じてトランザクション短縮、更新順序統一、SQL／索引改善、適切なtimeout、アプリ再試行を提案する。再試行はトランザクション単位の整合性と冪等性を評価する。

`pg_cancel_backend`は現在のクエリのキャンセルであり、idle in transactionの保持ロックを必ず解消する方法ではない。`pg_terminate_backend`は接続と進行中トランザクションへ影響する。必要な操作を具体化し、対象環境で許可された範囲で実行する。診断スクリプトはこれらを実行しない。

## 検証

2段以上の待機チェーン、idle in transaction、DDL待ち、prepared transaction、解消済みdeadlockを扱えるか確認する。現在のsnapshotで分かる範囲と過去ログから確認した事実を分け、待ち時間、deadlock率、トランザクション時間、利用者への影響を比較する。

## 公式資料

- [pg_locks](https://www.postgresql.org/docs/18/view-pg-locks.html)
- [pg_blocking_pids](https://www.postgresql.org/docs/18/functions-info.html)
- [ロックとdeadlock](https://www.postgresql.org/docs/18/explicit-locking.html)
- [ログ設定](https://www.postgresql.org/docs/18/runtime-config-logging.html)
