# 収集と入力

Python 3.10以降と、実DB収集では接続対象に対応したpsqlを使う。ローカルに配置したskillフォルダだけで動作する。認証はlibpq serviceと権限を限定したpassword file等、承認済みの仕組みで設定する。ファイルは利用者だけが読めるディレクトリに保存する。WindowsはディレクトリのACLも確認する。

```sh
PGSERVICE=security-observer python scripts/collect.py --target node-a --database app --output snapshots/app.json
python scripts/advisories.py --output snapshots/upstream.json
python scripts/review.py snapshots/app.json --contract app-contract.json --advisories snapshots/upstream.json --output reports/app.md
```

出力先の親ディレクトリは先に作る。既存ファイルを上書きしない。`advisories.py`はPostgreSQL公式2ページだけを取得する。解析はofflineで動き、7日より古いadvisoryは要追加調査とする。再現時は`--as-of YYYY-MM-DD`を使う。レポート比較には前回のJSONを`--previous`に渡す。以前の問題は、同じルール・対象で明示的な合格を確認したときに解消とする。

通常収集はセクションごとの読み取りtransactionで行う。同じラベルを別nodeへ再利用しない。接続ごとのnode識別、DB OID、postmaster起動時刻、ロール、版を照合する。各セクションは同時点とは限らない。対象DB一覧を使って未診断DBを確認し、各DBで収集を繰り返す。システムschemaと一時schemaを対象ACL一覧から除く。14以降で利用可能なcatalogを収集し、より古い版は版・DB・拡張の識別を試す。検証済みの範囲は[検証記録](validation.md)を参照する。

管理者が同じnode/DBで`--privileged`を付けて取得した分類済みsnapshotを`--supplement`に渡せる。取得間隔は1時間以内で、再起動やnodeの変更がないことを確認する。HBA options、エラー本文、接続文字列、verifier、policy式、関数定義は出力しない。SQLSTATEと収集状態はsnapshotに残る。`--format json`は識別子を含む私的なレビュー、`--format issue`は固定ルールIDと件数だけを出す共有用の下書き。

## アクセス契約の例

```json
{
  "target": "node-a",
  "database": "app",
  "runtime_roles": {
    "web": {
      "privileges": [
        {"object": ["relation", "app", "orders", ""], "privilege": "SELECT", "allowed": true},
        {"object": ["column", "app", "orders", "tenant_id"], "privilege": "UPDATE", "allowed": false}
      ]
    }
  },
  "deny_default_grants": [
    {"creator": "migration", "kind": "f", "grantee": "PUBLIC", "privilege": "EXECUTE"}
  ],
  "rls": [{"schema": "app", "table": "orders", "roles": {"web": ["select", "insert", "update"]}}],
  "routes": [
    {"id": "app to db", "database": "app", "role": "web", "address": "192.0.2.20",
     "transport": "tls", "require_tls": true, "require_identity_verification": true}
  ],
  "password_roles": {"web": {"method": "password"}, "job": {"method": "external"}}
}
```

ACLキーは `[kind, schema, name, signature]`。kindはdatabase/schema/relation/column/sequence/routine。databaseのschemaは空、schemaのnameはschema名、columnのsignatureは列名、routineは引数型リスト。sequenceのUSAGEやschemaのCREATEなども指定できる。`allowed: true`にgrant optionを許可する場合は`grantable: true`を追加する。アプリの実効接続ロールを列挙する。必要な管理操作はロールごとに`allow_flags`、`allow_groups`、`allow_admin`、`allow_ownership`で明示する。SECURITY DEFINERの公開を禁止するroutineは `restrict_public_routines` に [schema, name, signature] の配列で指定する。例外にはJSONレポートのfinding IDを使い、`exceptions`に`id`、`owner`、`reason`、`expires`を記録する。例外は表示に残る。

HBA transportはlocal/tls/plain/gss。許可しない接続は`allowed: false`、意図したtrustは`allow_trust: true`、物理レプリケーションは`physical_replication: true`を指定する。接続試験済みのrouteには`verification`と`verified`を追加する。`verified`は`connection_accepted`、`tls`、`plaintext_accepted`、`valid_identity_accepted`、`invalid_identity_rejected`の観測値。接続元ごと、proxyの各区間ごとに別routeとする。

`verification`のほか、任意の`runtime`、`distribution`、各`components`にも `target`、`database`、`server_version`（SHOW server_versionの値）、`observed_at`（ISO 8601）を含める。実物を確認していない項目は省略する。

- パスワードroleの`verification`には対象・時刻を入れ、同じroleの`lifecycle`に`generated`、`unique`、`default_removed`、`revocation_available`を真偽で記録する。
- `runtime`は`deployment`（self-managed/managed）、実プロセスの`process_uid`、起動時の`entrypoint_uid`、`rootless`、`user_namespace`、`privileged`、`docker_socket`、`unneeded_capabilities`、`unneeded_host_mounts`を受け付ける。ファイルは`files`に`id`、`kind`、`platform`、`mode`（8進文字列）、`owner_uid`、`service_uid`、調査済みの`unauthorized_access`を記録する。漏えいの所在は`secret_locations`に`kind`だけを入れる。
- `distribution`は`type`（upstream/vendor/managed）と、稼働中のpackage release・image digest・engine revisionを突き合わせて、その値を build に記録する。ベンダーの判定は`cves`にCVE IDごとの`state`（fixed/affected/not_affected）と公開HTTPSの`source`を記録する。延長サポートは`extended_support.active`と`extended_support.source`を確認する。
- `components`は`name`、`type`（extension/client等）、`version`、`state`（fixed/current/affected/unsupported）、公開HTTPSの`source`を記録する。sourceのURLにtoken等を含めない。ツールは担当者が入力した証拠の出典・対象・鮮度を表示し、ベンダーの判定自体は担当者が照合する。
