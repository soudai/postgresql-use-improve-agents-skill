# アプリロール・RLS・認証をレビューする

[English](../en/05-security-review.md) · [ユースケース一覧](README.md)

架空のシナリオとレポート例です。説明用に設定した観測内容であり、
実環境の検査結果やスクリプトの正確な出力ではありません。

## シナリオと提供する証拠

マルチテナントサービスで、`app.orders` とそのpartition
`app.orders_2026_11` にテナント分離が必要です。アクセス契約では
runtime・migration・管理を分けます。提供する観測はすべて
`node-a`、DB `app`、2026-11-06T01:00:00Zを対象とします。

| 利用者が提供する入力 | 主な証拠 |
| --- | --- |
| `security-snapshot.json` | 対象のロール・membership・ACL・policy・partitionの収集は完了。runtimeの `app_web` は、BYPASSRLSを持つ `ops_debug` にSET ROLE可能。 |
| `access-contract.json` | runtimeには自テナントのCRUDを許可し、管理操作とBYPASSRLSは許可しない。子への直接アクセスも親経由と同じテナント境界を守る。 |
| `policy-review.md` | 親経由にはtenant条件がある。子のRLSは有効で、schema/table権限により `app_web` の直接SELECTが可能。適用されるSELECT policyはpermissiveな `USING (true)` だけで、restrictiveなSELECT policyはない。 |
| `runtime-observations.json` | Unix上のPostgreSQLプロセスUIDは999。TLS鍵はUID 0所有・mode 0640だが、実効グループ・ACL・親パスのアクセス・承認済みreaderは未確認。 |
| `credential-review.md` | `app_web` の保存形式はSCRAM。有効期限はタイムゾーン付きの2026-12-01T00:00:00Z。生成方法・一意性・失効手段の証拠はない。 |

`policy-review.md` は管理者が限定範囲を私的に確認した資料です。
通常のcollectorは任意のpolicy式を出力しないため、この資料で
policyの意味を補います。tenant CRUDとTLS接続の試験は未実施です。

ファイル名は利用者が提供するexportや契約の仮名で、同梱ファイルではありません。
パスワード、verifier、秘密鍵、秘密入りDSNは含めないでください。

## コピーして使うプロンプト

```text
$postgres-security-review

提供した security-snapshot.json、access-contract.json、
policy-review.md、runtime-observations.json、credential-review.md を調べてください。
対象は node-a / app、評価時刻は 2026-11-06T01:00:00Z です。

app_webの実効権限とSET ROLE経路を確認してください。親経由と
app.orders_2026_11への直接アクセスを分け、適用policy、ACL、owner/FORCE、
BYPASSRLSを評価してください。確認した設定不備と、実際に観測した越境を区別してください。

TLS鍵は実プロセス主体と実効アクセスから評価してください。
SCRAM形式、認証方式、有効期限、資格情報のライフサイクルを分け、
verifierの形式からパスワード強度を推測しないでください。

指摘、unknown、根拠を私的なレポートにまとめてください。
管理接続を維持する限定的なロール・policy変更、隔離環境の受入試験、
復旧条件を提案してください。提供済みの証拠を使い、レビューできる変更計画を作成してください。
```

## 結果の記載例

| 項目 | 判定 | 理由と次の対応 |
| --- | --- | --- |
| runtimeのロール境界 | `finding` | 未承認のBYPASSRLSロールへSET ROLEできる経路を確認。membershipの利用者を調べ、対象を限定した取消や分離を計画する。 |
| 子への直接SELECT | `finding` | この経路ではRLS有効だけでは必要なtenant制限を満たさない。権限と適用policyは制限のないSELECTを許すが、実際の別tenant行の可視性は試験が必要。 |
| TLS鍵のアクセス | `unknown` | root所有0640は適切な構成になり得るが、このmetadataだけではサービスの読取可否と非承認readerの排除を確認できない。 |
| 資格情報のライフサイクル | `unknown` | SCRAM形式と期限内であることは分かる。生成方法、一意性、失効の準備は未確認。 |
| TLS接続の境界 | `unknown` | 鍵のmetadataは、hostname/CA検証や平文接続の拒否を証明しない。 |

提供した観測では、稼働中のPostgreSQLプロセスは非rootです。
サーバ鍵のroot所有は、サーバプロセスがrootで動いている意味ではありません。
脆弱なパスワードやデータ漏えいを観測したとは結論付けません。

改善案ではruntimeと管理ロールを分離し、契約に合わせて子への直接経路を制限します。
permissive policyに `true` があっても、適用されるrestrictive policyで
制限される構成なら判定は異なります。任意の式には追加のソース確認が必要な場合があります。

## 次に確認すること

1. 現在のmembership、owner、grant、policyを保存します。限定的な変更の
   前に、migration・監視・復旧手順の依存関係を確認します。
2. 使い捨て環境で、アプリのログイン、SET ROLE、poolを使ってtenant A/Bを
   試験します。正当なCRUD、越境拒否、tenant未設定、同じ接続のA→B再利用を含めます。
3. 親、子への直接経路、利用するview・routineで繰り返し確認します。
   新たに制限するSET ROLE経路も試験します。
4. 鍵本文を読まず、サービスの実効UID・グループ、鍵の所有者、ACL、親パスを
   確認します。結果が裏付ける場合だけ `service_readable=true` と
   `unauthorized_access=false` を記録します。
5. 秘密管理の運用から資格情報のライフサイクルを確認します。再発行する場合は
   旧資格情報の拒否と新資格情報の成功を試験します。
6. 正しいCA・hostnameでの接続、不正なidentityと平文の拒否、
   再起動、維持した管理経路を確認して再収集します。
   復旧時に、改善対象だった露出を無条件に戻さないようにします。

## 関連資料

- [Skill](../../skills/postgres-security-review/SKILL.md)
- [収集とアクセス契約](../../skills/postgres-security-review/references/evidence.md)
- [権限とRLS](../../skills/postgres-security-review/references/access.md)
- [認証・TLS・実行環境](../../skills/postgres-security-review/references/authentication.md)
- [変更と復旧](../../skills/postgres-security-review/references/changes.md)
