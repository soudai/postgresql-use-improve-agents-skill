# セキュリティ更新とバージョン移行の準備を調べる

[English](../en/06-upgrade-readiness.md) · [ユースケース一覧](README.md)

架空のレビューシナリオとレポート例です。最新リリースの案内、
実在する脆弱性情報、スクリプトの正確な出力ではありません。
以下のバージョンは比較用の入力であり、推奨更新先ではありません。

## シナリオと提供する証拠

ベンダービルドを使うサービスで、上流の更新状況とベンダーによる
セキュリティ修正を区別したいとします。復旧可能なメジャー移行も検討します。
この例の評価時刻は2026-11-06T01:00:00Zです。

| 利用者が提供する入力 | 主な証拠 |
| --- | --- |
| `security-snapshot.json` | 対象は `node-a`、DBは `app`。`server_version=17.11`、`server_version_num=170011`。 |
| `deployment-contract.json` | 稼働buildは架空の `17.11-example.7`。配布形態はベンダー管理で、未変更のupstream buildとは扱わない。 |
| `upstream-advisories.json` | 説明用の比較データにはminor 17.12と、上流での修正版が17.12であるadvisoryを掲載。実利用では公式URL、取得時刻、完全なrelease/advisory記録の検証が必要。 |
| `vendor-patch-review.md` | 仮定上の限定レビューで、そのadvisoryのbackportと稼働buildが一致。この例には実在するベンダーや根拠URLを掲載しない。 |
| `migration-rehearsal.md` | 17から18への論理移行のリハーサルで、切替後に新側で10件の注文を受付。旧側にはその書込みがなく、逆同期もない。 |
| `recovery-contract.md` | 要件は受付済み注文の欠損0件、RPO 0。RTOは30分。復元・再適用の時間は未計測。 |

以下の「advisory A」は説明用の呼称で、実在するCVEでも、
機械入力のadvisory識別子として使える値でもありません。実レビューでの
backport判定には、対象に一致する真正な公開根拠があることが条件です。

ファイル名は、自分で提供するexportやレビュー資料の仮名で、
同梱の入力ファイルではありません。再現のために公式やベンダーの根拠を
作らないでください。offlineの比較ケースでは最新advisory入力がないか、
評価時刻の10日前に取得したcacheだけが残っているとします。

## コピーして使うプロンプト

```text
$postgres-security-review

提供した security-snapshot.json、deployment-contract.json、
upstream-advisories.json、vendor-patch-review.md、migration-rehearsal.md、
recovery-contract.md を 2026-11-06T01:00:00Z 時点でレビューしてください。

上流minorの新旧、コミュニティサポート、ベンダーサポート、
個別advisoryの該当性・修正状況を分けてください。
backportの主張は稼働package/buildと公開根拠を照合し、
一つの主張からすべての修正を推測しないでください。
根拠の欠落や期限切れはunknownとして残し、再取得する項目を示してください。

実際のrelease notes、拡張・client互換性、停止時間、復旧要件から、
サポート対象minorへの更新とメジャー移行を比較してください。
移行リハーサルでは、旧側に存在しない新側の注文10件と、
RPO 0・RTO 30分の要件を考慮してください。

根拠付きレポート、段階的な変更・復旧計画、停止条件、受入試験を提示してください。
新側の書込み後に接続先だけ戻すことを、切戻しの完了条件にしないでください。
```

## 結果の記載例

| 確認事項 | 判定例 |
| --- | --- |
| 上流minorは最新か | 提供した比較データでは17.11より17.12が新しい。更新日程を決める前に、実際にサポートされる更新先とrelease notesを確認する。 |
| advisory Aは修正済みか | 真正な公開backport根拠が一致する場合、その稼働buildについて修正済みと判定できる。上流minorが古いことだけでは、この限定判定と矛盾しない。 |
| すべての脆弱性に対応済みか | unknown。一つのbackport判定では、他advisory、拡張、client、設定上の露出を確認できない。 |
| offlineの場合はどうか | feedがない場合、または10日前の情報しかない場合、最新release/advisory状況はunknown。証拠がないことを「脆弱性なし」と扱わない。 |
| 接続先だけ戻す切戻しは可能か | 不可。受付済みの注文10件が旧側に存在せず、RPO 0に違反する。 |
| 復旧時間の要件を満たすか | unknown。RTO 30分を満たすことは復旧リハーサルで計測する必要がある。 |

最新のfeedと検証済みのbackportから、「上流の新しいminorを検討する」と
「この個別advisoryは修正済み」は両立します。コミュニティのサポートと
ベンダーによる延長サポートは、それぞれ日付付きの根拠が必要です。
サポート終了majorの最終minorを、そのまま適切な更新先とはしません。

## 次に確認すること

1. 稼働package・image・engine revision、版情報の整合性、対象の同一性を確認します。
   公式release/security情報を再取得し、ベンダーの個別advisory、
   影響component、build、サポートの根拠を照合します。
2. 候補版のrelease notesと拡張・driver・pooler・HAの対応を確認し、
   実際にサポートされる更新・移行方式を選びます。
3. 別環境にバックアップを復元し、データの意味、アプリ・管理接続、
   ロール、ACL、RLS、禁止操作の拒否を試験します。
4. 切替前に書込み停止・排出、同期確認、停止条件を決めます。
   戻す判断の前に、試験済みの再適用や逆同期の方式で
   新側が受け付けたすべての書込みを保持できるようにします。
5. 失敗後は方式に応じ、再修正、復元・再適用、再同期を選びます。
   `pg_upgrade` なら選択した転送モードを確認します。
   linkとswapには、旧clusterの再利用について追加の制限があります。
6. 選択した復旧をリハーサルし、注文10件がすべて保持されることを確認します。
   RPO 0とRTO 30分を実測し、代表負荷も再検証して、
   準備完了とする前に日付付きの証拠を残します。

## 関連資料

- [セキュリティ診断Skill](../../skills/postgres-security-review/SKILL.md)
- [入力、advisoryの鮮度、ベンダー契約](../../skills/postgres-security-review/references/evidence.md)
- [バージョン・移行・復旧](../../skills/postgres-security-review/references/changes.md)
- [負荷検証Skill](../../skills/postgres-workload-validation/SKILL.md)
