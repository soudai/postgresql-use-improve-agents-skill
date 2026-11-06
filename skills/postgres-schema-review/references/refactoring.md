# 変更計画と検証

## 変更案を具体化する

診断結果ごとに対象と根拠、分割後の1行の意味、多重度、整合性条件、変更対象のコード・DDL、移行中の読書き先、停止条件、切戻し可能な時点を示す。自動生成した一般的な手順を、そのまま本番へ適用しない。既に許可された変更範囲は引き継ぎ、診断だけの依頼からDDL適用や外部投稿へ拡張しない。

注文・決済・配送の分割では、再決済や分割配送が1:Nになり得る。現在状態を表す表を残す場合は、事実の履歴との同期責任、再構築方法、遅延の許容範囲を決める。複数業務の独立性と、同時に確定する必要がある不変条件の両方を確認する。

## 構造ごとの移行条件

- FK: 孤児・型・参照先の候補キー、tenant境界、NULLと削除動作を確認する。対象版・表の種類が対応する場合、NOT VALIDで新規違反を防止し、既存データを修復してVALIDATEする。未検証期間を完了扱いしない。
- NOT NULL/CHECK: 正常な状態と業務上の必須性を確認し、既存行を段階的に修正する。列とdomainの制約を区別し、空のスカラー副問い合わせ由来のNULLも試験する。NOT VALIDでは新規違反の拒否と既存行の検証を分け、修復後のVALIDATEまで確認する。
- 一意性: 重複、NULLの扱い、並行書込みを確認する。CREATE UNIQUE INDEX CONCURRENTLYを選ぶ場合、transaction block外で実施する。失敗した無効索引の扱い、構築中の負荷・待機、制約への関連付けに使える索引の条件を対象版で確認する。
- 改名・分割: ORM、SQL、view、routine、BI、ETL、CDC、replica、backup、外部APIを調べる。依存する権限、owner、RLS、policy、trigger、identity/sequence、索引も移管する。
- backfill: 範囲・進捗・再実行性・新旧の競合解決を定める。二重書込みは失敗時の部分成功を検出し、整合性を回復できるようにする。新旧両方のアプリが存在する期間を試験する。

ロックの種類、table rewrite、partitionの制約などは実サーバの版で確認する。[ALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html)、[CREATE INDEX](https://www.postgresql.org/docs/18/sql-createindex.html)

## 検証する結果

1. 同じ業務操作で同じ意味の結果になる。件数だけでなく関連、多重度、NULLの意味、訂正履歴、状態遷移を比較する。
2. 禁止した参照・NULL・状態の組合せが拒否される。アプリロールで正当な操作とRLS境界を確認する。
3. 並行操作と再試行でも重複・欠落・二重処理が発生しない。移行途中で中断・再開した場合も確認する。
4. 代表負荷で待機時間、p95/p99、失敗率、再試行、JOIN増加と書込み負荷を比較する。
5. 再収集で制約のvalidated/enforcedを確認し、契約と観測を更新して再診断する。

切戻しでは、新側で受け付けた書込みを旧構造へどう戻すかまで定める。旧列や表の削除は互換期間と復旧条件を満たした後に計画する。バックアップの存在は復元できた証明にはならない。

## 本skillの受入試験

単体テストは問題・正常・証拠不足の判定を扱う。実DB試験はローカルにある公式imageから新しい隔離コンテナを作り、合成データだけを使用する。

```sh
python -m unittest discover -s tests -v
python tests/schema_scenarios.py --image postgres:18-bookworm
python tests/schema_scenarios.py --image postgres:19rc1-bookworm
```

実DB試験はFK追加と検証前後、複合tenant参照、domain型NULLの保存と列NOT NULLによる拒否、NOT NULL NOT VALIDの既存NULL・新規拒否・修復・VALIDATE、FKを持たない外部IDの必須性、nullable CHECK、正当なboolean、quoted identifier、収集打切り、最小権限で見えない統計を確認する。対応版と実測した範囲はPRの検証記録へ記載する。業務固有の分割が正しいかどうかは、その業務の整合性・負荷試験で検証する。

## 実行した検証

2026-11-06、WSL Ubuntu-24.04とローカルの公式PostgreSQL imageで確認した。

- schema単体テスト111件が成功。domain/列制約、NOT VALID、外部ID、旧snapshot、複数列NULL集計、通常継承での保証範囲を含む。
- PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) と19rc1 (Debian 19~rc1-1.pgdg12+1)で統合試験が成功。19はpreviewの検証結果。
- FK追加、tenantを含む複合参照、NOT VALIDからVALIDATEへの移行と再診断を確認。MATCH FULLと一部NOT NULLによるNULL拒否も実DBで確認した。
- domain NOT NULLだけでは空のスカラー副問い合わせからNULLが保存されること、列NOT NULL追加後に同じ書込みが拒否されることを確認。NOT NULL NOT VALIDで既存NULLが残る一方、新規NULLと未修復のVALIDATEが拒否され、修復・検証後の再診断で指摘が解消することを確認。
- ネストしたdomain CHECK、NULLを許すCHECK、正当なnullable FK・Boolean、quoted identifier、収集上限、非特権ロールの統計欠測を確認。
- 同じ注文行に対する決済・配送の2セッションで実際の待機を観測し、独立した表へ分離した後は双方の更新が待機せず進むことを確認。これは合成シナリオであり、任意の業務で分割が有効だと保証する結果ではない。
- 式・行に置いた検証用literalが収集・レポートに出ないこと、入力エラーで値を漏らさないことを確認。
- skill形式検証と独立した利用テストが成功。利用テストでは業務資料・静的SQL・traceから入力を作り、競合、分割候補、不変性の証拠不足を区別した。
- MATCH FULLの推論は検証・強制済みの列保証に限定した。旧snapshot v1のnot_null=trueは保証に昇格せず、再収集を求める。外部IDのFK例外とNULL禁止は独立して報告する。

自動SQL AST解析、本番での行プロファイル収集、任意アプリの移行・負荷検証はこの実装には含まれない。設計意図、実際の変更列とtransactionの対応は入力する証拠で確認する。条件付き必須のNULLは、追加調査として業務状態と整合性をレポートへ補足する。
