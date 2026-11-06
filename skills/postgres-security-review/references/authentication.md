# 認証・通信・実行環境

[HBA](https://www.postgresql.org/docs/current/auth-pg-hba-conf.html)は最初に一致した行で認証する。file viewは現在のファイルを示す。実際にロードした順序やquoted keyword、+role、regex、@リスト、DNS、ident/peer mapping、外部認証の成否は、必要な範囲だけ管理者と確認する。ルールの一致は候補で、firewall・proxy・アクセス制御の証拠と新規接続で裏付ける。オプション全文やLDAP等の秘密を転記しない。

- `ssl=on`はTLSを利用できる設定。要求のある各区間で、新規TLS接続成功、TLSなしの拒否、CAとhostnameの検証成功、不正なCAや名前での拒否を確認する。[libpq TLS](https://www.postgresql.org/docs/current/libpq-ssl.html)を参照し、別driverは当該driverの検証設定を確認する。Unix socketやGSSの許可経路も契約に含める。
- HBA変更前に管理接続と復旧用の設定を確保する。既存pool接続の状態と、新規接続の結果を分ける。適用方法はreload結果とログを確認し、Windowsの後続接続への反映を考慮する。証明書期限、CAローテーション、レプリケーションや管理経路を影響範囲に含める。
- [Password Authentication](https://www.postgresql.org/docs/current/auth-password.html)に沿って、生成設定、保存形式、実際の認証方式を別々に確認する。HBAがmd5でもSCRAM verifierならSCRAMが選ばれる。NULL verifierのパスワード認証は失敗する。VALID UNTILは外部認証の期限を示さない。SCRAMでもパスワード強度は未確認の場合がある。
- SCRAM移行はclientとpoolerの対応確認、生成方式の変更、秘密管理経由での再発行・配布、HBA変更、新規接続テストの順。漏えい時は旧資格情報を失効させ、既存sessionも対処範囲に入れる。[passwordcheck](https://www.postgresql.org/docs/current/passwordcheck.html)は導入制約とpre-encrypted入力への制限を確認する。

## OS・コンテナ・保存先

対象と紐付けた観測をアクセス契約のruntimeへ記録する。管理者がLinux上で調べるときは、対象PIDの `ps -o euid= -p PID` と、対象を限定した `stat -c '%u %a' -- PATH` でmetadataを確認できる。Dockerは、対象の `docker inspect --format '{{.HostConfig.Privileged}}'` 等で必要なフィールドだけを見る。User/entrypointと、実プロセス・capabilities・socket/mount・daemonのrootlessやuser namespaceを区別する。[PostgreSQLの起動](https://www.postgresql.org/docs/current/server-start.html)、[公式イメージのentrypoint](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh)、[Docker rootless](https://docs.docker.com/engine/security/rootless/)を参照する。

PGDATA、WAL、tablespace、設定、TLS鍵、ログ、バックアップ、password fileの実効アクセスを調べる。Unix modeは共有グループ・親ディレクトリ・ACLも確認する。サーバ鍵のroot所有0640は許容される構成があるため、グループとpostgresの読取を確かめる。WindowsではACL、managedでは提供元の責任分界、object storageでは共有・暗号化・復元に必要な権限を確認する。

argv、環境、CI、設定、FDW、subscriptionの秘密露出が疑われる場合は、権限のある担当者が所在だけを分類する。環境変数、接続文字列、鍵の内容のダンプは避ける。影響する秘密を更新し、必要な書込み先だけ許可して、起動、接続、保守、バックアップを隔離環境で再検証する。
