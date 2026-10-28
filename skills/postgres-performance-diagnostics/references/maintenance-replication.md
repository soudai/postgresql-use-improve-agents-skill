# VACUUM・肥大化・WAL・レプリケーション

## autovacuumと肥大化

テーブル統計、relation／index／TOASTサイズ、vacuum／analyze履歴、進捗、autovacuumログ、設定を使う。更新量に処理能力が追い付かないのか、古いsnapshotによって回収できないのかを分ける。

確認する要素はテーブル別threshold／scale factor、worker数、cost制限、ロックによる中断、長いトランザクション、prepared transaction、replication slot、standbyのfeedback。autovacuumの最終時刻が古いだけで異常にせず、変更量と必要性を確認する。

`analyze.py`の`current`は次の2つで切り分けを助ける。

- `autovacuum_backlog`: `n_dead_tup`、`n_ins_since_vacuum`、`n_mod_since_analyze`が、reloptionsを反映した起動閾値（PostgreSQL 18の`autovacuum_vacuum_max_threshold`を含む）を超えたテーブル。launcherは`autovacuum_naptime`ごとに巡回するため、比が1を少し超えるのは一時的に正常。大きく超えたまま残るなら、worker不足、cost制限（待機サンプルの`Timeout:VacuumDelay`）、ロックによる中断、回収不能のどれかを調べる。insert閾値は18以降の未freeze割合の補正を含まない近似。
- `xmin_horizon_holders`: このDBのセッションのxmin／xid、prepared transaction、replication slotのxmin・catalog_xmin、standbyのfeedbackをageの降順に並べる。最上位が回収を止めている候補。catalog_xminはシステムカタログだけに効く。他DBのセッションはこのDBのユーザーテーブルの回収を止めない。

パーティションの親テーブルはautovacuumがANALYZEしない。`partitioned_parent_never_analyzed`は、パーティション全体の推定に使う親の統計がない状態を示す。親に対する定期的な`ANALYZE`を検討する。

PostgreSQL 18は`pg_stat_user_tables`の`total_autovacuum_time`等でVACUUM・ANALYZEの累積時間を示す。区間差分で、保守に使った時間と更新量の関係を確認できる。

`n_dead_tup`は推定値であり、物理的な肥大率やOSへ返せる容量ではない。表の中で再利用できる空き領域とファイルを縮小して返す領域を区別する。急激なサイズ変化には通常のデータ増加やTOASTも含める。

必要なら`pgstattuple`や`pgstattuple_approx`を使う。導入・実行の権限と提供環境の対応を確認し、全表走査／I/Oコストが大きい操作は対象サイズと実行時間帯を絞る。approxも常に低負荷とは限らない。索引は方式に応じた検査を選び、B-tree向け情報を他方式へそのまま適用しない。

HOT更新の割合は区間の更新数を母数にする。更新列と索引、ページ内の空き領域、fillfactorを確認する。低HOT率だけでは不適切な索引設計と断定しない。全行を更新する負荷では、fillfactor 100の表はHOT率がほぼ0になり、fillfactor 70では空き領域に収まる分（約4割）がHOTになる（PostgreSQL 18／19rc1のシナリオで確認）。visibility mapとIndex Only ScanのHeap Fetchesも併せて確認する。

PostgreSQL 18ではVACUUMされていないページのHeap Fetchesが毎回発生し、VACUUMで解消する。19rc1では、Index Only Scanがheapを読んだときにvisibility mapが更新され、VACUUMなしでも2回目以降のHeap Fetchesが0になることを観測した。19ではHeap Fetchesが初回の計画だけに現れるため、取得した計画が初回かどうかを確認する。

XID／MXID ageはwraparound対策の緊急性に関わる。設定されたfreeze上限、進行速度、進捗、回収を妨げる保持元を確認し、通常の性能改善と区別して優先度を示す。

対策にはテーブル単位のautovacuum調整、トランザクション短縮、更新設計、ANALYZE、索引再作成等がある。`VACUUM FULL`は強いロック・書き直し・追加容量を伴う。高いdead tuple推定だけで機械的に提案しない。通常VACUUMで再利用が進むことと、ファイル縮小を区別して説明する。

## WALとチェックポイント

同じ区間の`pg_stat_wal`、`pg_stat_checkpointer`、`pg_stat_bgwriter`、`pg_stat_io`、SQL別WAL、checkpointログ、更新量とストレージ指標を使う。WAL bytes/秒、FPI、checkpoint頻度・時間・書込量を確認する。

PostgreSQL 18のWAL I/O timingは`pg_stat_io`の`object=wal`等、実際のビュー定義に沿って取得する。旧版の列を固定して使わない。`track_wal_io_timing`が無効だった区間の時間0は未計測。backendの種類やI/O操作を区別し、同じ処理を重複計上しない。

SQLの更新量増大、索引による書き込み増幅、頻繁なcheckpoint、ストレージの遅延を切り分ける。WAL量はデータ更新サイズと一対一ではない。FPIやcheckpoint周期の影響を含め、同じ負荷条件で比較する。

checkpoint／WAL設定の候補は、書き込みの平準化、WAL容量、復旧時間、バックアップ／レプリケーション要件を併記する。耐久性を落とす`fsync`・`full_page_writes`・同期commit等の変更を既定の高速化策にしない。

## レプリケーションとWAL保持

primary／standbyの役割、同期方式、物理／論理の構成を確認する。replication、receiver、slot、subscription、archive、競合統計、LSNと空き容量を使う。観測時刻を揃え、送信・受信・flush・replayのどこで止まるかを調べる。

lag値を「追い付くまでの予測時間」として説明しない。トラフィックがない時のNULLは直ちに障害を意味しない。同期レプリケーションによるcommit待ちと、非同期standbyのreplay遅延は影響が異なる。

slotはactive状態、restart_lsn、xmin／catalog_xmin、保持WAL、消費側の状態を確認する。inactiveなslotや停止したarchiveによって容量が増える場合がある。論理レプリケーションではdecodingのspill、適用worker、エラー、競合も調べる。slotのsnapshot保持がVACUUMへ及ぼす影響も確認する。

standbyではクエリ競合、replay、読み取り負荷、feedbackのprimaryへの影響を扱う。slot削除、同期条件の緩和、WAL保持上限等は再同期やデータ保護に影響するため、復旧手順と業務要件を含めた変更案にする。

## 検証

| ケース | 確認する違い |
| --- | --- |
| 大量更新でautovacuumが追い付かない | worker／cost／thresholdと更新量 |
| 古いsnapshotで回収できない | 実行頻度を増やしても解決しない保持元 |
| 再利用可能な空き領域 | ファイル縮小とは区別 |
| checkpoint過多と更新量増大 | 発生率・FPI・I/O・SQL更新量 |
| 停止slotによるWAL蓄積 | 消費停止・容量・再同期要件 |
| 同期commit待ちとreplay遅延 | 利用者影響と詰まる段階 |

性能と併せて、vacuum所要時間、age、relationサイズ、WAL生成・保持量、commit応答時間、レプリケーション遅延、復旧要件を比較する。

## 公式資料

- [Routine Vacuuming](https://www.postgresql.org/docs/18/routine-vacuuming.html)
- [pgstattuple](https://www.postgresql.org/docs/18/pgstattuple.html)
- [WAL configuration](https://www.postgresql.org/docs/18/wal-configuration.html)
- [Standbyとreplication slots](https://www.postgresql.org/docs/18/warm-standby.html)
- [累積統計](https://www.postgresql.org/docs/18/monitoring-stats.html)
- [PostgreSQL 18の変更点](https://www.postgresql.org/docs/18/release-18.html)
