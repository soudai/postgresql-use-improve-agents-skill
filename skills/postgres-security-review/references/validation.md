# 検証

テストはローカルに取得済みの公式イメージを使い、ネットワークなしのコンテナ内でループバック接続する。DBポートの公開や本番のデータマウントは行わない。マイナー更新試験だけ、テストが作成した一意なボリュームを二つのバージョンで順に使う。

```sh
python -m unittest discover -s tests
python tests/security_scenarios.py --image postgres:18-bookworm --major-source postgres:17-bookworm --minor-source postgres:18
python tests/security_scenarios.py --image postgres:19rc1-bookworm
python tests/security_regressions.py --image postgres:18-bookworm
python tests/security_regressions.py --image postgres:19rc1-bookworm
```

`--minor-source`には同一majorの古いタグまたはdigestを指定する。ローカルの `postgres:18` は実施時に18.1で、更新先の `postgres:18-bookworm` は18.6だった。両タグが同じminorを指す場合は旧イメージを明示する。

- 18.6と19 RC1のセキュリティ試験は、最小権限と補完取得、採取上限、runtimeからの間接SET ROLE、PUBLIC権限、暗黙WITH CHECK、A/Bの許可と拒否、view/partition/routineの別経路、一時表での名前解決、既定権限、SCRAM、TLSとhostname検証、HBA include・未reload・構文不正・旧設定への復帰を扱う。
- 17.11から18.6への論理dump/restoreで、ロールとテーブルを復元し、RLSとアクセス境界を検証する。ロールdumpはパスワードを含めない。移行先でHBAを構成し直し、禁止TCP接続の拒否を確認する。18.1から18.6は同じ試験用データディレクトリを使い、新しいバイナリで起動後にデータアクセスとHBAによる接続拒否を再検証する。
- security_regressions.pyは18.6と19 RC1で、RLS有効の子への越境読取、restrictive policyでのCRUD制限、SET ROLE後のowner/FORCE/BYPASSRLS、root所有鍵のサービス読取不可・グループ共有・読取制限と実TLS接続、同日中に失効したSCRAM資格情報の新規接続拒否を確認する。単体試験は同じ反例が修正前に失敗することを確認して追加した。
- 単体試験は未確認TLS、IPv6 HBA、OS rootと非root実プロセス、補完snapshotの不一致、CVEとvendor build、期限切れ情報、preview、比較の未確認扱いを確認する。
- 独立したskill動作確認では、部分的な権限・認証データとvendorの修正申告を使い、公開案の匿名化と未確認の扱いを確認した。見つかったTLS部分観測と稼働build証拠の問題を回帰テストにした。

ベンダー延長サポート、Windows ACL、Docker daemonのrootless、OS固有の実行権限は利用者が確認したmetadataを扱う。実環境のprovider API、Windows実機、HA切替、物理レプリケーションによるメジャー移行、pg_upgradeの各転送方式はこの試験の対象外。19 RC1はpreviewとして識別する。


## Issueの受入確認（2026-11-06）

既存シナリオに加え、完了条件を次の試験へ対応付けた。18/19の試験は18.6と19rc1で実行し、合成データとテスト専用の資格情報だけを使う。

```sh
python tests/security_collection_acceptance.py --image postgres:18-bookworm
python tests/security_collection_acceptance.py --image postgres:19rc1-bookworm
python tests/security_access_acceptance.py --image postgres:18-bookworm
python tests/security_access_acceptance.py --image postgres:19rc1-bookworm
python tests/security_auth_acceptance.py --image postgres:18-bookworm
python tests/security_auth_acceptance.py --image postgres:19rc1-bookworm
python tests/security_versions_acceptance.py --source postgres:17-bookworm --image postgres:18-bookworm
```

| Issue | 実装・受入の根拠 |
| --- | --- |
| #23 収集・レポート | collection acceptanceはSET LOCAL前の有効値/source、最小権限・特権不足・採取上限、未収集DB、秘密の非出力を検証。アプリ側に同名operatorを置いても通常収集で実行しない。単体では列欠落による部分失敗、対象変更、timeout/SQLSTATEの秘匿を確認 |
| #24 実効権限 | access acceptanceはINHERIT/SETの区別、列ACL・grant option、NULL ACL/PUBLIC、新規table/sequence/routineの既定権限と復元を実接続で確認。既存regressionsはSET ROLE後のowner/FORCE/BYPASSRLSも確認 |
| #25 RLS | 公開表の正常なRLS省略、default deny、同一接続のA→B→識別子なし、越境INSERT/UPDATE/DELETE拒否。既存シナリオとregressionsでview/routine/子への別経路とrestrictive policyを確認 |
| #26 HBA・TLS | auth acceptanceはIPv4/IPv6、正しいCAとhostname、不正CA/hostname、非TLSの許可・拒否を確認。既存シナリオはinclude・未reload・構文不正と管理経路の復旧を確認 |
| #27 パスワード | MD5のみ対応するclient fixtureを実serverへ接続し、MD5認証成功とSCRAM非互換を確認。再発行後の既存接続継続、旧秘密の新規接続拒否、接続排出と新秘密での再接続を確認。単体でNULL/外部認証/NOLOGIN/未確認、保存形式と強度管理の分離を確認 |
| #28 OS・秘密保護 | 所有者/modeの局所変更後もサービスを再起動し、接続・書込み・backup/restoreが成功。非承認readerのアクセスを拒否。rootというDBロール名、rootless、capabilities、Windows ACL/provider証拠は単体で別扱いを確認 |
| #29 DEFINER | PUBLICにCREATEを許したschemaでの名前解決差替えと修正、正当な公開関数、owner/ACL/search_pathの記録・復元を実DBで確認。未取得の本文・動的到達先は引き続き追加調査 |
| #30 版・CVE | 固定日時の単体で出典欠落、期限切れ、未知版、EOL/間近、延長support、backport、preview、server/client/extension分離を確認。修正版への更新は対象CVEの明示的passとして前回比較で解消を追跡 |
| #31 復旧・更新 | 既存シナリオで17.11→18.6の論理復元と18.1→18.6のマイナー更新、権限/RLS/認証拒否の維持を確認。versions acceptanceでは新側の業務書込みが旧側へ存在しないことも確認し、単純な接続先切戻しによる喪失を実証 |

MD5-only clientはprotocolの互換境界を調べるfixtureであり、特定の旧libpq/driverの実機ではない。保持・再接続するpsqlはpool内の接続の挙動をモデル化し、特定pooler製品の検証とは区別する。Windows/managed service/rootless daemonはmetadata fixtureの範囲で、実機確認済みとはしない。

独立したskill利用評価では、公開FAQのRLS省略、稼働buildとbackport証拠、最新advisoryのないoffline入力、移行後に新側へ書込み済みの復旧案、診断のみの依頼を扱った。DBやGitHubは変更せず、許可範囲と未確認項目を含むレポートを作成した。そこで見つかったロール属性の欠測をfalseとして扱う問題は回帰テストを追加して修正した。
