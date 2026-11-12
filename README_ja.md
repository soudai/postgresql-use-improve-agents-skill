# PostgreSQLの診断・レビュースキル

[English](README.md) | [日本語](README_ja.md)

PostgreSQLの性能診断、設定・テーブル設計・セキュリティのレビュー、代表負荷での変更検証を行う5つのAgent Skillです。観測結果から、問題候補、不足する根拠、変更案、検証手順をまとめます。

まずは[日本語の利用例](example/ja/README.md)または[English examples](example/en/README.md)を参照してください。skill本体の指示と詳細な参照資料は主に日本語で、ユースケース別の例は日英両方を用意しています。

## 目的に合うskillを選ぶ

| 目的 | Skill | 主な入力 |
| --- | --- | --- |
| 遅いSQL、待機、I/O、ロック、VACUUM、WAL、レプリケーション | [postgres-performance-diagnostics](skills/postgres-performance-diagnostics/SKILL.md) | 2時点の統計、待機サンプル、計画、ログ、アプリのtrace |
| メモリ、接続数、checkpoint、autovacuum、プランナ、JITの設定 | [postgres-config-tuning](skills/postgres-config-tuning/SKILL.md) | 現在の設定、利用可能な資源、負荷、区間レポート |
| 制約漏れ、命名、ライフサイクル、責務、競合しやすいテーブル | [postgres-schema-review](skills/postgres-schema-review/SKILL.md) | catalogのsnapshot、業務上の契約、運用上の根拠 |
| ロール、RLS、認証、TLS、実行環境の権限、セキュリティ更新 | [postgres-security-review](skills/postgres-security-review/SKILL.md) | catalogのsnapshot、アクセス契約、接続経路ごとの根拠 |
| 変更前後の結果一致、応答時間、処理量、資源使用量 | [postgres-workload-validation](skills/postgres-workload-validation/SKILL.md) | 同じ条件で複数回取得した実行ログ、SLO、検証方針 |

必要に応じて、症状の診断、対象を絞った変更案、効果検証へと組み合わせて使います。テーブル設計やセキュリティのレビューは、取得済みのsnapshotから単独で始めることもできます。

## オフラインの例を試す

この例はPython 3.10以降だけで実行できます。環境によっては`python`を`python3`に置き換えてください。リポジトリのルートで次を実行してください。

```text
python skills/postgres-workload-validation/scripts/compare_runs.py example/inputs/workload/baseline.json example/inputs/workload/candidate.json --p95-ms 20 --min-tps 4 --max-loss-rate 0 --metric-max oom_kills=0
```

JSONに`"decision": "rollback_candidate"`が出力されます。変更後の合成データでは応答時間の値が小さくなっていますが、処理結果が一致していないためです。資源指標の欠測と試行不足もレポートに残ります。このコマンドはローカルファイルを解析し、PostgreSQLへの接続や切り戻し操作は行いません。

小さなサンプルは判定処理の動作確認用です。ベンチマークの実測値や、性能を評価するのに十分な根拠ではありません。[プロンプトと結果の詳しい例](example/ja/07-workload-validation.md)を参照してください。

## エージェントで使う

`skills/`から必要なskillのフォルダ全体を、利用するエージェントのskill探索ディレクトリへ配置します。含まれる`SKILL.md`、`scripts/`、`sql/`、`references/`、`assets/`は一緒に配置してください。既存の同名skillがある場合は、差分を確認してから置き換えます。探索先と呼び出し構文はエージェントによって異なります。

利用例では`$skill-name`の形式で呼び出します。この構文に対応するエージェントへの依頼例です。

```text
$postgres-performance-diagnostics を使い、before.json と after.json を
レビューしてください。対象APIのp95目標は200msです。利用者への影響順に
候補を並べ、観測事実と仮説を分けてください。変更案に進む前に必要な
追加情報と、検証・切り戻しの手順も示してください。
```

このプロンプトの`before.json`と`after.json`は利用者が用意するファイルです。上記の負荷検証サンプルを除き、利用例に登場するファイル名は準備する入力を示しており、データセットを同梱しているわけではありません。

## ユースケース別の例

各ページに前提、コピーして使えるプロンプト、レポート例、次に確認する項目を記載しています。01〜06は仮想の観測値に基づくエージェントの回答例で、CLIの出力そのものではありません。

| ユースケース | 英語 | 日本語 |
| --- | --- | --- |
| 遅いクエリ、行数推定、索引の仮説 | [01](example/en/01-slow-query.md) | [01](example/ja/01-slow-query.md) |
| ロックの待機連鎖とactivityの可視性不足 | [02](example/en/02-lock-contention.md) | [02](example/ja/02-lock-contention.md) |
| checkpoint、JIT、対象を限定した設定比較 | [03](example/en/03-configuration-review.md) | [03](example/ja/03-configuration-review.md) |
| 制約、命名、テーブルのライフサイクル | [04](example/en/04-schema-review.md) | [04](example/ja/04-schema-review.md) |
| 最小権限、RLS、パスワード、TLS | [05](example/en/05-security-review.md) | [05](example/ja/05-security-review.md) |
| セキュリティ更新の根拠とアップグレード準備 | [06](example/en/06-upgrade-readiness.md) | [06](example/ja/06-upgrade-readiness.md) |
| 応答時間が短くても結果が不一致の変更 | [07](example/en/07-workload-validation.md) | [07](example/ja/07-workload-validation.md) |

## 自分の環境の根拠を収集・解析する

以下のコマンドはリポジトリのルートで実行します。オフラインの解析はPython 3.10以降と標準ライブラリで動作します。実DBからの収集には、接続対象に対応する`psql`とアクセス権限も必要です。SQL単位の累積統計には、利用できる場合に`pg_stat_statements`を使います。取得できない項目は制限として報告します。

接続先は`PGSERVICE`などのlibpq設定、認証は既存のパスワードファイル等で指定します。インスタンスのラベルには、秘密を含まない安定した識別子を使います。性能診断の収集例です。

```text
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/before.json
```

代表的な負荷が流れた後に、同じ接続先から再度収集して区間差分を解析します。

```text
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/after.json
python skills/postgres-performance-diagnostics/scripts/analyze.py snapshots/before.json snapshots/after.json --format json --output reports/interval.json --top 20
```

再起動、統計のreset、failover、ロールや負荷の変更によって比較できなくなることがあります。必要に応じて比較可能なsnapshotを採り直します。収集権限・上限、待機サンプル、計画・ログについては[収集手順](skills/postgres-performance-diagnostics/references/collection.md)と[診断の進め方](skills/postgres-performance-diagnostics/SKILL.md)を参照してください。

設定レビューでは、アプリと同じ接続ロール・DBで設定を収集します。以下の資源量は例なので、対象環境の実測値に置き換えてください。

```text
psql -X -qAt --set=instance_id=prod-primary --file skills/postgres-config-tuning/sql/settings.sql > snapshots/settings.json
python skills/postgres-config-tuning/scripts/review_config.py snapshots/settings.json --ram-gb 64 --cpus 16 --storage ssd --workload oltp --max-active-connections 80 --interval-report reports/interval.json --output reports/config.md
```

不明な資源量は指定を省略し、managed serviceでは`--managed`を使います。上で作成したJSONの区間レポートをレビューの根拠として渡せます。変更計画は[適用・検証・切り戻し](skills/postgres-config-tuning/references/apply-and-verify.md)を参照してください。

その他の入力と手順は次のとおりです。

| 手順 | 準備 |
| --- | --- |
| テーブル設計レビュー | [収集と入力形式](skills/postgres-schema-review/references/evidence.md)を参照し、[契約例](skills/postgres-schema-review/assets/contract.example.json)と[証拠例](skills/postgres-schema-review/assets/evidence.example.json)を業務要件に合わせて作成します。 |
| セキュリティレビュー | [収集とアクセス契約](skills/postgres-security-review/references/evidence.md)に沿ってDBごとに収集し、未確認の接続経路、実行環境、ベンダーの更新情報を明らかにします。 |
| 負荷検証 | [負荷の再生と比較](skills/postgres-workload-validation/references/replay.md)に沿って、代表性のある条件と証拠の採用基準を定めてから複数回計測します。 |

各スクリプトの引数は`--help`で確認できます。同梱のオフライン例を除き、コマンド中のファイルは収集先または利用者が用意する入力です。セキュリティの収集では出力先の親ディレクトリを先に作成し、既存ファイルは上書きしません。

## 結果の読み方

- 欠測、古い証拠、件数上限、権限不足は、判定不能や制限として報告します。負荷0、問題なし、合格と同一には扱いません。
- Seq Scan、NULLを許す列、Booleanのflag、更新されるテーブルは文脈と照合します。構造と業務上の不変条件や実測した挙動を結び付けて判断します。
- 要求checkpointの数だけではWAL圧迫を確定できません。JITコンパイル時間の比率だけでJIT無効化を決めません。
- RLSのフラグ、SCRAMのverifier形式、ファイルmodeだけでは、実効的な分離、パスワードの強度、認められた主体だけのアクセスを証明できません。
- 負荷検証のレポートv2は、結果一致、性能、証拠の質、改善の成立を分けます。`accept_candidate`には明示的な`--policy`とその採用基準を満たす証拠が必要です。SLO合格だけでは因果的な改善を確定しません。

収集はcatalogと統計の読み取りを基本とします。`EXPLAIN ANALYZE`は対象SQLを実行し、負荷の再生はデータを書き換える場合があります。DDL、設定変更、再起動、セッション終了、負荷の再生は、具体的な検証・復旧手順を用意し、依頼で許可された範囲で行います。

資格情報はコマンド、プロンプト、Git管理するファイルに記載しないでください。識別子、SQL、計画、ログにも機密情報が含まれ得るため、収集・共有は必要な範囲に限定します。`snapshots/`と`reports/`はGitの対象外ですが、ファイルのアクセス権限が制限されるわけではありません。

## 対応版と検証

性能診断・設定・テーブル設計はPostgreSQL 18・19を対象としています。記録済みの統合テストにはPostgreSQL 18.6と19 RC1が含まれます。これは検証したビルドの記録で、最新リリースの説明ではありません。利用するサーバ、拡張、managed serviceの実際の仕様で検証してください。セキュリティ収集は14以降で版に応じたcatalogを取得し、より古い版では識別・版情報の取得を試みます。すべての版の完全な対応を意味しません。

待機サンプルとテーブル設計のsnapshotはschema version 2です。旧形式では、判断に必要な根拠が不足することがあります。[待機イベントの解釈](skills/postgres-performance-diagnostics/references/wait-events.md)、[テーブル設計の入力互換性](skills/postgres-schema-review/references/evidence.md)、[負荷検証の出力項目](skills/postgres-workload-validation/references/replay.md)を参照してください。

リポジトリのルートでユニットテストを実行します。

```text
python -m unittest discover -s tests -v
```

統合テストにはDockerと、ローカルに取得済みの公式イメージが必要です。

```text
python tests/integration.py --image postgres:18-bookworm
```

統合テストは合成データを使う使い捨てコンテナで動作し、ネットワークを無効化し、ホストのポートやデータを公開・マウントしません。イメージは自動取得しません。詳しいシナリオ、実行要件、過去の結果は[性能診断の検証記録](skills/postgres-performance-diagnostics/references/validation.md)、[セキュリティの検証記録](skills/postgres-security-review/references/validation.md)、[テーブル設計の変更検証](skills/postgres-schema-review/references/refactoring.md)を参照してください。

## ディレクトリ構成

```text
skills/                  5つの独立したskillとスクリプト・参照資料
example/en/              英語のプロンプト・レポート例
example/ja/              日本語のプロンプト・レポート例
example/inputs/workload/ 実行可能な比較用の合成データ
tests/                   ユニットテスト・隔離環境のDBシナリオ
README.md                英語ガイド
README_ja.md             日本語ガイド
```

## ライセンス

[MIT License](LICENSE)
