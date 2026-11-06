# 権限・テナント分離

実アプリのログインロールとSET ROLE後のロールを確認する。契約にはruntime、migration、監視、運用を分け、必要な操作と対象を示す。アクセス契約がないDBで全表のRLSを要求する根拠はない。

- [Role Membership](https://www.postgresql.org/docs/current/role-membership.html)に沿って、通常のACL継承、SET ROLEの到達経路、ADMIN OPTIONを分ける。SUPERUSER、BYPASSRLS、CREATEROLE、CREATEDB、REPLICATIONはログインとSET ROLEで到達する実効ロールの属性を確認する。属性未収録やrole行欠落をfalseとして合格にせず、unknownと不足項目を残す。危険な属性を確認できた場合は他の欠測があっても指摘を維持する。16以降は各membershipのINHERIT/SET/ADMINを使い、旧版の詳細は該当版の資料と実接続で確認する。
- NULL ACLではPostgreSQLの[組み込み既定値](https://www.postgresql.org/docs/current/ddl-priv.html)を評価する。DB CONNECT/TEMP、schema USAGE/CREATE、table、列、sequence、routineの権限を操作ごとに組み合わせる。判定はACLの証拠であり、接続可否やSQL成功はHBA、schema、policy等も含め実接続で確認する。
- ownerはACLやpolicyを変更できる。既存ACLと[作成ロール別のデフォルト](https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html)を個別に保存する。NOLOGIN owner、migration、runtimeを分け、GRANT/REVOKEは対象とgrantorを限定する。sequenceや移行後の新規objectも試す。
- `parameter_acl`は設定のSET/ALTER SYSTEMの権限の証拠。機能要件と許可範囲を確認する。カタログの一般的なアクセス制御やすべての拡張の暗黙権限は個別調査する。

## RLS

[Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)に沿って、RLSが必要な表、適用role、操作、tenant IDの信頼元を決める。RLSの有効フラグとpolicyを照合する。RLS有効で適用できるpermissive policyがない場合はdefault deny。ALL/UPDATEではWITH CHECK省略時のUSINGも評価する。permissiveのORとrestrictiveのAND、UPDATEで必要なSELECT policy、ACL、ownerのFORCE、SUPERUSER/BYPASSRLSを確認する。TRUNCATE/REFERENCESはACLで管理する。

親を通るアクセスと、[子への直接アクセス](https://www.postgresql.org/docs/current/ddl-inherit.html)を分ける。直接アクセスでは子自身のRLS・policyが適用されるため、有効フラグのある子もschema/table権限、実効ロール、owner/FORCE/BYPASSRLS、操作別policyを確認する。子のpermissiveにtrueがあっても、適用されるrestrictiveが絞るなら漏えいと断定しない。任意の式や関数は子・ロール・操作を指定したunknownとし、その経路のtenant CRUD試験へ進む。default denyのpassは当該子の当該操作だけを意味し、親や別操作の安全性は含まない。[policyの合成と操作条件](https://www.postgresql.org/docs/current/sql-createpolicy.html)に沿い、UPDATE/DELETEの具体的な文で必要なSELECT policyも試す。

view所有者とsecurity_invoker、materialized viewのコピー、SECURITY DEFINERの所有者も調べる。viewの依存関係は候補であり、view自身のフィルタを別途確認する。SQL関数、動的SQL、外部コードの到達先は、許可されたソースから追加調査する。

隔離したテストDBでA/Bの合成データを作り、実アプリと同じログイン、ロール、接続プールで検証する。Aは自分の行を読める。Bの行は見えず、BへのINSERT、tenant変更UPDATE、BのDELETEが拒否または0行になる。tenant IDなし/NULL、同じ接続をA→Bに再利用するケース、view/routine/partition経由も試す。クライアント自身が設定できるGUCを認可の根拠にする場合は、誰がその値を変更できるかを確認する。

## SECURITY DEFINER

[公式の安全な関数定義](https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY)を参照する。呼出者のEXECUTEとschema USAGE、ownerの権限、search_path上のschema CREATE、TEMP権限を照合する。PG15以降に更新したDBにも過去のpublic ACLが残り得るため実ACLを使う。security_barrierとsecurity_invokerの目的を分ける。

到達可能な候補は、本文を私的に調べ、名前の修飾、動的SQLの値のパラメータ化と識別子の安全な組み立て、信頼できるschemaと末尾pg_temp、ownerの最小権限を確認する。新規関数の作成・PUBLIC EXECUTE取消・必要なGRANTを同じtransactionで行う。影響が分からない外部コードや拡張管理objectは、提供元と依存アプリを確認する。testで同名一時表・書込可能schemaの差し替えと正当な呼出を検証する。


## 受入検証

tests/security_access_acceptance.pyは、ローカル取得済みのPostgreSQL 18.6 / 19rc1をネットワーク分離した使い捨て環境で使い、次を実接続で確認する。既存のtests/security_scenarios.pyとtests/security_regressions.pyの子partition・view・FORCE/BYPASSRLS・CRUD検証を補完する。

- 多段membershipのINHERIT/SET差分、特殊な管理属性が通常ACLのようには継承されないこと、管理用途の許可例外を確認する。列のgrant optionは他roleへの付与と取消後の拒否効果まで確認する。
- NULL ACLの組み込みPUBLIC権限と、作成者の既定権限変更後に作ったtable・sequence・routineのアプリ成功／別role拒否を確認する。限定したgrantの復元後も、実効権限を再検査する。
- RLSを要件としない公開表を除外し、保護対象のdefault denyをデータ露出と取り違えない。同一接続でA→B→識別子未設定の順にtransactionを再利用し、SET LOCALの残留がないことを確認する。この試験は実際の認証基盤によるtenant識別子の信頼性を証明しない。
- 更新前のDBから引き継いだpublic schemaのCREATEを合成して名前解決の差替えを再現し、所有者・ACL・search_pathの修正後に正当な呼出と禁止呼出を確認する。限定環境で旧状態の復元と再修正も実演する。これは漏えい状態の本番復旧を推奨するものではない。

明示修飾された公開関数でも、catalogだけでは本文・動的SQL・外部言語の安全性全体を証明できない。レポートのunknownを残し、実アプリが使う経路と入力を追加検証する。
