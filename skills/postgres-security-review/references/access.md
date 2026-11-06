# 権限・テナント分離

実アプリのログインロールとSET ROLE後のロールを確認する。契約にはruntime、migration、監視、運用を分け、必要な操作と対象を示す。アクセス契約がないDBで全表のRLSを要求する根拠はない。

- [Role Membership](https://www.postgresql.org/docs/current/role-membership.html)に沿って、通常のACL継承、SET ROLEの到達経路、ADMIN OPTIONを分ける。SUPERUSER、BYPASSRLS、CREATEROLE、CREATEDB、REPLICATIONは実効ロールの属性を確認する。16以降は各membershipのINHERIT/SET/ADMINを使い、旧版の詳細は該当版の資料と実接続で確認する。
- NULL ACLではPostgreSQLの[組み込み既定値](https://www.postgresql.org/docs/current/ddl-priv.html)を評価する。DB CONNECT/TEMP、schema USAGE/CREATE、table、列、sequence、routineの権限を操作ごとに組み合わせる。判定はACLの証拠であり、接続可否やSQL成功はHBA、schema、policy等も含め実接続で確認する。
- ownerはACLやpolicyを変更できる。既存ACLと[作成ロール別のデフォルト](https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html)を個別に保存する。NOLOGIN owner、migration、runtimeを分け、GRANT/REVOKEは対象とgrantorを限定する。sequenceや移行後の新規objectも試す。
- `parameter_acl`は設定のSET/ALTER SYSTEMの権限の証拠。機能要件と許可範囲を確認する。カタログの一般的なアクセス制御やすべての拡張の暗黙権限は個別調査する。

## RLS

[Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)に沿って、RLSが必要な表、適用role、操作、tenant IDの信頼元を決める。RLSの有効フラグとpolicyを照合する。RLS有効で適用できるpermissive policyがない場合はdefault deny。ALL/UPDATEではWITH CHECK省略時のUSINGも評価する。permissiveのORとrestrictiveのAND、UPDATEで必要なSELECT policy、ACL、ownerのFORCE、SUPERUSER/BYPASSRLSを確認する。TRUNCATE/REFERENCESはACLで管理する。

親を通るアクセスと子partitionへの直接接続、view所有者とsecurity_invoker、materialized viewのコピー、SECURITY DEFINERの所有者を調べる。viewの依存関係は候補であり、view自身のフィルタを別途確認する。SQL関数、動的SQL、外部コードの到達先は、許可されたソースから追加調査する。

隔離したテストDBでA/Bの合成データを作り、実アプリと同じログイン、ロール、接続プールで検証する。Aは自分の行を読める。Bの行は見えず、BへのINSERT、tenant変更UPDATE、BのDELETEが拒否または0行になる。tenant IDなし/NULL、同じ接続をA→Bに再利用するケース、view/routine/partition経由も試す。クライアント自身が設定できるGUCを認可の根拠にする場合は、誰がその値を変更できるかを確認する。

## SECURITY DEFINER

[公式の安全な関数定義](https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY)を参照する。呼出者のEXECUTEとschema USAGE、ownerの権限、search_path上のschema CREATE、TEMP権限を照合する。PG15以降に更新したDBにも過去のpublic ACLが残り得るため実ACLを使う。security_barrierとsecurity_invokerの目的を分ける。

到達可能な候補は、本文を私的に調べ、名前の修飾、動的SQLの値のパラメータ化と識別子の安全な組み立て、信頼できるschemaと末尾pg_temp、ownerの最小権限を確認する。新規関数の作成・PUBLIC EXECUTE取消・必要なGRANTを同じtransactionで行う。影響が分からない外部コードや拡張管理objectは、提供元と依存アプリを確認する。testで同名一時表・書込可能schemaの差し替えと正当な呼出を検証する。
