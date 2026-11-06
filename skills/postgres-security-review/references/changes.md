# セキュリティ変更と復旧

変更案には、対象、変更前の状態と適用元、実行する差分、依存先、ロック/停止、停止条件、確認する操作、復旧手順を含める。権限変更ではowner、grantor、grant option、membership、default privileges、policyを保存する。認証変更では接続継続と復旧用の経路を確認する。権限を戻すとデータが再露出する場合は、経路の遮断などの封じ込めを維持したまま業務を回復する。

| 検証 | 期待結果 |
| --- | --- |
| 新規アプリ接続、管理接続、レプリケーション | 必要な接続と操作が継続する |
| 意図しないロール、禁止経路、別tenant、切替前の資格情報 | 許可契約に従って拒否される |
| 代表データ、書込み、バッチ・migration、バックアップ | 結果と権限境界を維持する |
| 再診断、監査ログ、変更記録 | 対象、日時、実行者、差分、結果を追跡できる |

ログは必要な失敗・変更イベントと閲覧・保存期間を確認する。SQLや秘密の全文を監査証拠に含めない。アプリ処理への影響は同じ代表負荷で確認し、利用可能なら `postgres-workload-validation` で結果と応答時間を比較する。

## バージョンとセキュリティ情報

[公式の対応期限](https://www.postgresql.org/support/versioning/)と[アドバイザリ](https://www.postgresql.org/support/security/)を取得した日を記録する。server_version_numは上流の比較に使い、ディストリビューションやprovider独自版のパッチは稼働中のパッケージrelease、image digest、engine revisionで照合する。CVEの成立条件と影響componentを個別advisoryで確認する。コミュニティEOLとmanagedの延長サポートは別項目。拡張とclientはそれぞれのmaintainerの資料を確認する。

## 適用・移行

- マイナー更新では現在majorの修正版とrelease notesを確認し、停止・バイナリ更新・再起動・追加作業とHAへの順序を計画する。
- メジャー移行ではdump/restore、pg_upgrade、レプリケーションを停止時間、容量、拡張・driver互換性、データ検証で比較する。pg_upgradeを使う場合は新バイナリで、選択した転送モードを指定して `--check` を実行する。提供元が管理する場合はその事前チェックとサポート対象に従う。
- バックアップを別環境へ復元し、アプリ・管理接続、ACL、RLS、データと拒否ケースを試す。RPO/RTO、切替前の書込み停止、切替後の書込みを引き継ぐ方法、旧環境の破棄時点を決める。
- pg_upgradeのlink方式では新cluster起動後の旧データ再使用を避ける。swap方式は転送途中から旧clusterが変更される。copy/cloneや論理移行でも新側の書込みは旧側へ自動反映されない。変更失敗後は採用方式に応じて再修正、復元、再同期を選ぶ。

参照: [Upgrading a PostgreSQL Cluster](https://www.postgresql.org/docs/current/upgrading.html)、[pg_upgrade](https://www.postgresql.org/docs/current/pgupgrade.html)、[Backup and Restore](https://www.postgresql.org/docs/current/backup.html)。
