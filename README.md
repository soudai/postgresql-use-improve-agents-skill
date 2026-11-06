# PostgreSQL Performance Diagnostics Agent Skill

PostgreSQL 18・19の統計情報、実行計画、ログを使い、改善するSQLやテーブルを根拠付きで絞り込むAgent Skillです。[全体計画Issue #1](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/1)の診断を、共通の収集基盤と用途別の手順にまとめています。

## 内容

- `skills/postgres-performance-diagnostics/SKILL.md`: エージェントの入口と調査の分岐。
- `references/`: 待機イベント、SQL・索引、I/O・メモリ、ロック、保守・レプリケーションの診断手順。
- `sql/`: 読み取り用の収集SQLと、その依存ビューを記録するマニフェスト。
- `scripts/`: スナップショット収集、待機サンプリング、区間差分、変更前後の比較、実行計画、JSONログ、アプリtraceの解析。
- `skills/postgres-config-tuning/`: 設定（postgresql.conf・ロール単位・parameter group）の見直しと変更計画を作る別スキル。

統計から出せるのは調査候補です。Seq Scan、低ヒット率、高い実行回数だけで異常とは判定せず、実行計画やアプリの情報で原因を確認します。DDL・設定変更・セッション終了を自動適用するツールではありません。

## 利用要件と対応版

- Python 3.10以降。解析スクリプトは標準ライブラリで動作します。
- 実DBからの収集には`psql`と接続情報が必要です。PostgreSQL 18以降のクライアントを推奨します。
- 基本統計は標準機能を使用し、SQL単位の累積統計には`pg_stat_statements`を使用します。未導入・権限不足の収集項目は、その制限を報告します。
- 対象はPostgreSQL 18・19です。2026-10-26に確認した[公式19ドキュメント](https://www.postgresql.org/docs/19/index.html)はRC1です。実サーバの版、ビュー・列、拡張版を検出し、19正式版公開後にも検証してください。

## スキルを利用する

利用するエージェントのスキル探索ディレクトリへ、`skills/postgres-performance-diagnostics`をフォルダごと配置します。配置先はエージェントごとに確認してください。以下はCodex用のコピー例です。既存の同名スキルがある場合は、上書きせず差分を確認してください。

```powershell
$skillSource = Join-Path (Get-Location) 'skills/postgres-performance-diagnostics'
$skillTarget = Join-Path $env:USERPROFILE '.codex/skills/postgres-performance-diagnostics'
if (Test-Path -LiteralPath $skillTarget) { throw '同名スキルが既にあります。差分を確認してください。' }
New-Item -ItemType Directory -Path (Split-Path $skillTarget -Parent) -Force | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Recurse
```

呼び出し例:

```text
$postgres-performance-diagnostics を使い、before.json と after.json から
改善候補を抽出してください。対象APIのp95目標は200msです。
実行計画が不足する候補は、必要な追加情報と取得手順を示してください。
```

## 統計を収集・比較する

接続先は`PGSERVICE`または`PGHOST`・`PGPORT`・`PGDATABASE`・`PGUSER`等、認証はlibpqのパスワードファイルなどで設定します。パスワードをコマンドラインやリポジトリへ記載しないでください。

```powershell
$env:PGSERVICE = 'performance-observer'
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/before.json
```

代表的な負荷が流れた後、同じ接続先・同じ識別子で再度収集します。

```powershell
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/after.json
python skills/postgres-performance-diagnostics/scripts/analyze.py snapshots/before.json snapshots/after.json --format markdown --output reports/interval.md --top 20
```

`--instance-id`は接続先を識別する安定した名前にし、別クラスタに同じ名前を使わないでください。統計のreset、再起動、ロール切替、設定変更があった区間は、その影響を確認して新たな基準点を取得します。

SQL本文は収集時の`--include-query-text`指定時だけ含めます。本文を含めない場合も、relation名、ユーザー名、application_name等は機密情報になり得ます。出力は権限を限定した場所に保存してください。`snapshots/`と`reports/`はGitの対象外です。

区間レポートには、top-level実行時間に占める割合（`exec_time_share`）、論理読み取り・WAL量・計画時間の順位、DB単位の平均アクティブセッション数に加え、時点の候補として`xmin_horizon_holders`（VACUUMの回収を止めている保持元）、`redundant_indexes`（重複・先頭一致の索引）、`autovacuum_backlog`（起動閾値を超えたテーブル）が含まれます。

## 待機をサンプリングする

累積統計では、時間がCPU・I/O・ロックのどこで使われたかは分かりません。症状が出ている間に`pg_stat_activity`をサンプリングし、待機の内訳とquery_idごとの割合を集計します。

```powershell
python skills/postgres-performance-diagnostics/scripts/sample_activity.py --instance-id prod-primary --samples 120 --interval 1 --format markdown --output reports/waits.md
```

1つの接続で読み取りSELECTと`pg_sleep`を交互に実行します。SQL本文は取得しません。解釈は[待機イベント](skills/postgres-performance-diagnostics/references/wait-events.md)を参照してください。

取得項目、拡張導入、権限、タイムアウト、収集上限については[収集手順](skills/postgres-performance-diagnostics/references/collection.md)を参照してください。スクリプトの正確な引数は各`--help`でも確認できます。

## 計画とログを解析する

```powershell
python skills/postgres-performance-diagnostics/scripts/analyze_plan.py plan.json --format json --output reports/plan.json
python skills/postgres-performance-diagnostics/scripts/analyze_logs.py postgresql.jsonlog --format json --output reports/logs.json
```

`plan.json`はJSON形式のEXPLAIN出力です。ログ解析はPostgreSQLの`jsonlog`を対象とします。`--format`の既定はMarkdownです。式やSQL・ログ本文まで出力する場合は`--include-details`を指定します。EXPLAIN ANALYZEはSQLを実際に実行するため、まず取得済みの計画を使い、追加実行が必要なときは対象・負荷・副作用を評価します。詳しくは[クエリ診断](skills/postgres-performance-diagnostics/references/query-tuning.md)を参照してください。

自動解析は候補抽出を補助します。計画解析は全ての最適化を網羅せず、ログのtemp・duration等のメッセージ解析は英語形式を対象とします。deadlockはSQLSTATEでも判別します。未分類ログや部分採取がある場合は、解析結果の制限事項を確認してください。

## 設定を最適化する

`skills/postgres-config-tuning`は、ハードウェア・ワークロード・観測結果から設定の変更案を作る別スキルです。診断スキルと同様にフォルダごと配置します。現在の設定は`pg_settings`全体を、資格情報・コマンド・パスを伏せてJSONで取得します。

```powershell
psql -X -qAt --file skills/postgres-config-tuning/sql/settings.sql > settings.json
python skills/postgres-config-tuning/scripts/review_config.py settings.json --ram-gb 64 --cpus 16 --storage ssd --workload oltp --max-active-connections 80 --interval-report reports/interval.json --output reports/config.md
```

`--interval-report`には診断スキルの`analyze.py --format json`の出力を渡し、checkpointの要求比率、WAL量、temp量、JIT時間を根拠にします。メモリ・CPU・ストレージを指定しない項目は推測せず「評価対象外」として出力します。managed serviceでは`--managed`を指定します。設定は変更せず、反映方法・検証・切り戻しは[適用と検証](skills/postgres-config-tuning/references/apply-and-verify.md)にまとめています。

## 診断とIssueの対応

| Issue | 診断 |
| --- | --- |
| [#2](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/2) | 共通収集・拡張導入・版の差分 |
| [#3](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/3) | スロークエリ・非効率なスキャン |
| [#4](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/4) | キャッシュ・ブロックヒット率・I/O |
| [#5](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/5) | temp spill・ソート・ハッシュ |
| [#6](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/6) | JOIN・ORDER BY・LIMITと索引 |
| [#7](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/7) | 大量行取得・高頻度SQL・N+1 |
| [#8](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/8) | ロングトランザクション・ロック・deadlock |
| [#9](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/9) | 見積もり誤差・統計不足・計画退行 |
| [#10](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/10) | autovacuum・肥大化・freeze・HOT |
| [#11](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/11) | WAL・checkpoint・レプリケーション |
| [#12](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/12) | 接続・CPU・メモリ・並列実行 |

## 検証

```powershell
python -m unittest discover -s tests -v
```

ユニットテストには本番DBの認証情報は不要です。実サーバでの検証では、PostgreSQLと拡張の正確な版、成功した収集項目、スキップした項目を記録してください。性能改善の評価手順と、診断レポートの形式は[検証手順](skills/postgres-performance-diagnostics/references/validation.md)にあります。

Dockerによる統合テストは、使用する公式イメージをローカルに取得してから実行します。

```powershell
docker pull postgres:18-bookworm
python tests/integration.py --image postgres:18-bookworm
docker pull postgres:19rc1-bookworm
python tests/integration.py --image postgres:19rc1-bookworm
```

各Issueの問題例・正常例・情報不足例と変更前後の比較は、シナリオテストで再現します。CPUを2個に制限した使い捨てコンテナと、OOM確認用にメモリを640MBに制限したコンテナを使い、10分程度かかります。

```powershell
python tests/scenarios.py --image postgres:18-bookworm --report reports/scenarios-18.json
python tests/scenarios.py --image postgres:19rc1-bookworm --only 3 9 --report reports/scenarios-19.json
```

`--only`で対象のIssue番号を絞れます。結果のJSONには、各確認項目の成否と、変更前後の計測値（実行時間、WAL量、HOT率等）が残ります。

実ストレージのI/O待ち（#4・#12）を含めるには、Dockerホストで使い捨てPostgreSQLボリュームを支えるブロックデバイスを`--io-device`に指定します。cgroup v2のI/O制御が必要です。以下の`/dev/sda`は例です。

```text
docker run --pull=never --rm --network none --entrypoint findmnt postgres:18-bookworm -n -o SOURCE -T /var/lib/postgresql
python tests/scenarios.py --image postgres:18-bookworm --only 4 9 12 --io-device /dev/sda --report reports/scenarios-18.json
```

OOM検証にはcgroup v2のメモリイベントも必要です。I/O検証はテストコンテナだけに読み取り帯域制限を設定し、実際に`PGDATA`のデバイスに適用されたことを確認します。`--io-device`の指定がない場合は、I/O待ちの実ストレージ検証をスキップしたことをJSONに記録します。

テストは新しい使い捨てコンテナを作り、合成データと`pg_stat_statements`を使います。ホストのポート・データディレクトリは公開／マウントせず、コンテナのネットワークを無効化します。テスト自身が作ったコンテナだけを識別して終了時に削除します。スクリプトはイメージを自動取得しません。

PostgreSQL 18.6／19 RC1で実行した統合テストとシナリオの結果は[検証記録](skills/postgres-performance-diagnostics/references/validation.md)を参照してください。

## 代表負荷で変更を検証する

[postgres-workload-validation](skills/postgres-workload-validation/SKILL.md)を使って、業務負荷を再生する条件とSLOを揃えます。pgbenchの個別ログやアプリの観測イベントを使い、複数試行の応答時間・処理量・失敗・未送信・結果の一致を比較します。

追加診断の実DB検証は`python tests/followups.py --image postgres:18-bookworm`または`--image postgres:19rc1-bookworm`で実行できます。ローカルのイメージから使い捨てのDBを起動します。

## PostgreSQLのセキュリティをレビューする

[postgres-security-review](skills/postgres-security-review/SKILL.md)は、DBの権限・RLS・認証と、実行環境・パッチ状況を根拠付きでレビューします。DBの読み取り情報と、実アプリが許可される操作や接続経路の契約を照合し、確認できた問題と追加調査が必要な項目を分けます。

```sh
mkdir -p snapshots reports
PGSERVICE=security-observer python skills/postgres-security-review/scripts/collect.py --target node-a --database app --output snapshots/app-security.json
python skills/postgres-security-review/scripts/advisories.py --output snapshots/upstream.json
python skills/postgres-security-review/scripts/review.py snapshots/app-security.json --contract app-contract.json --advisories snapshots/upstream.json --output reports/app-security.md
```

契約例、限定した管理者収集、前回比較、公開用Issue案は[収集と入力](skills/postgres-security-review/references/evidence.md)を参照してください。改善案には変更後の許可・拒否の確認と復旧手順を含めます。[検証手順と範囲](skills/postgres-security-review/references/validation.md)には隔離環境でのTLS、権限、バックアップ復元と更新の試験を記載しています。


## セキュリティ監査の反例に対する検証

RLSは子テーブルへの直接アクセスも実効ロールと操作ごとに調べ、restrictive policy等の任意の条件式から隔離の保証を断定しません。TLS鍵はmode・所有者だけで合格にせず、サービスの実効読取可否と非承認主体のアクセス確認が必要です。`review.py --as-of`はタイムゾーン付き日時を受け付け、未指定では現在UTC時刻を使います。日付だけを渡した場合、同日中のパスワード失効はunknownです。

```sh
python tests/security_regressions.py --image postgres:18-bookworm
python tests/security_regressions.py --image postgres:19rc1-bookworm
```

試験はネットワークを分離した使い捨てDBで、子のRLSによる許可・拒否、TLS鍵の実効アクセス、失効した資格情報の新規接続拒否を確認します。詳細は[検証手順](skills/postgres-security-review/references/validation.md)を参照してください。

## テーブル設計をレビューする

[postgres-schema-review](skills/postgres-schema-review/SKILL.md)は、FKの不足、命名、NULL・flagの意味、ライフサイクル・責務の混在、不変にすべき事実の更新、同一行への更新競合を診断します。対象schemaのcatalogと、業務の設計契約・読み書きの観測を照合し、確定した不一致・改善候補・追加調査・根拠付き例外を区別します。Issue [#33](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/33)〜[#37](https://github.com/soudai/postgresql-use-improve-agents-skill/issues/37)に対応します。

```powershell
python skills/postgres-schema-review/scripts/collect.py --schema public --output snapshots/schema.json
python skills/postgres-schema-review/scripts/review.py snapshots/schema.json --contract contract.json --evidence evidence.json --format markdown --output reports/schema.md
```

[設計契約の例](skills/postgres-schema-review/assets/contract.example.json)と[観測情報の例](skills/postgres-schema-review/assets/evidence.example.json)を、実際の業務資料・コード・traceに合わせて用意します。入力形式、収集範囲と証拠の作り方は[入力手順](skills/postgres-schema-review/references/evidence.md)を参照してください。JSON形式のレポートも出力できます。

collectorはPython標準ライブラリとpsqlで単独利用でき、DDLや行データを自動操作しません。設計意図とtraceの正規化には業務知識が必要です。観測していない処理、収集上限、権限による統計欠測をレポートに残します。現在状態の表、nullable FK、独立したBooleanの存在だけでは違反としません。

```powershell
python tests/schema_scenarios.py --image postgres:18-bookworm
python tests/schema_scenarios.py --image postgres:19rc1-bookworm
```

検証した範囲と移行時の確認は[変更計画と検証](skills/postgres-schema-review/references/refactoring.md)を参照してください。


## 証拠を限定した診断と互換性

不明な情報を0や合格として扱わず、観測した事実と原因の仮説を分けて報告します。

- activity sampleは出力v2です。権限やactivity追跡の不足があるとAAS・peakはnullとなり、観測できた下限と不足理由を別に出します。旧sampleを再集計する場合も追跡状態の証拠がなければ不明になります。
- 計画の`$n`だけではgeneric planと断定しません。`--plan-mode`と`--plan-source`で採取時の根拠を記録します。BRINのlossy bitmapは本来の性質であり、メモリ不足の証拠とは限りません。`--relation-evidence`で同じ対象の索引方式を補えます。
- 設定のcheckpoint要求比率は原因の追加調査、JIT比率は同条件ON/OFF試験の候補です。単独の比率から変更値や効果を断定しません。
- schema snapshot v2は列とdomainのNOT NULL、制約の検証・強制状態を区別します。v1も読めますが、合成された`not_null: true`を列の保証として使わず、再収集を求めます。設計契約・観測情報の形式はv1のままです。
- workload report v2は正しさ、観測したSLO、証拠の十分性を分けます。旧入力も読めますが、採用候補には`--policy`で代表負荷と試行数・件数・時間・ばらつきの検証計画が必要です。欠測があっても確認できた違反は残し、中央値の差だけでは因果的な改善を断定しません。

監査の反例を再現する追加試験（ローカルの使い捨てDBのみ）:

```sh
python tests/performance_evidence_scenarios.py --images postgres:18-bookworm
python tests/performance_evidence_scenarios.py --images postgres:19rc1-bookworm
python tests/config_workload_evidence_scenarios.py --image postgres:18-bookworm
python tests/config_workload_evidence_scenarios.py --image postgres:19rc1-bookworm
```

schemaの反例は`tests/schema_scenarios.py`に含みます。短い負荷試験は診断とログ解析の動作確認であり、本番の性能改善や採用の根拠ではありません。
