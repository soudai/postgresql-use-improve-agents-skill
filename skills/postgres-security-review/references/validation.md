# 検証

テストはローカルに取得済みの公式イメージを使い、ネットワークなしのコンテナ内でループバック接続する。DBポートの公開や本番のデータマウントは行わない。マイナー更新試験だけ、テストが作成した一意なボリュームを二つのバージョンで順に使う。

```sh
python -m unittest discover -s tests
python tests/security_scenarios.py --image postgres:18-bookworm --major-source postgres:17-bookworm --minor-source postgres:18
python tests/security_scenarios.py --image postgres:19rc1-bookworm
```

`--minor-source`には同一majorの古いタグまたはdigestを指定する。ローカルの `postgres:18` は実施時に18.1で、更新先の `postgres:18-bookworm` は18.6だった。両タグが同じminorを指す場合は旧イメージを明示する。

- 18.6と19 RC1のセキュリティ試験は、最小権限と補完取得、採取上限、runtimeからの間接SET ROLE、PUBLIC権限、暗黙WITH CHECK、A/Bの許可と拒否、view/partition/routineの別経路、一時表での名前解決、既定権限、SCRAM、TLSとhostname検証、HBA include・未reload・構文不正・旧設定への復帰を扱う。
- 17.11から18.6への論理dump/restoreで、ロールとテーブルを復元し、RLSとアクセス境界を検証する。ロールdumpはパスワードを含めない。移行先でHBAを構成し直し、禁止TCP接続の拒否を確認する。18.1から18.6は同じ試験用データディレクトリを使い、新しいバイナリで起動後にデータアクセスとHBAによる接続拒否を再検証する。
- 単体試験は未確認TLS、IPv6 HBA、OS rootと非root実プロセス、補完snapshotの不一致、CVEとvendor build、期限切れ情報、preview、比較の未確認扱いを確認する。
- 独立したskill動作確認では、部分的な権限・認証データとvendorの修正申告を使い、公開案の匿名化と未確認の扱いを確認した。見つかったTLS部分観測と稼働build証拠の問題を回帰テストにした。

ベンダー延長サポート、Windows ACL、Docker daemonのrootless、OS固有の実行権限は利用者が確認したmetadataを扱う。実環境のprovider API、Windows実機、HA切替、物理レプリケーションによるメジャー移行、pg_upgradeの各転送方式はこの試験の対象外。19 RC1はpreviewとして識別する。
