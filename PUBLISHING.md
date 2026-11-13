# Claudeプラグインディレクトリへの公開手順

メンテナ向けの手順です。このリポジトリのルートがプラグインのフォルダ（`.claude-plugin/plugin.json`を含む）です。公開すると公式マーケットプレイス`claude-plugins-official`に掲載され、利用者は自分でマーケットプレイスを追加せずに導入できます。

公式の手順は[Submit a plugin](https://claude.com/docs/plugins/submit)と[提出前チェックリスト](https://claude.com/docs/plugins/pre-submission-checklist)です。内容が異なる場合は公式ドキュメントに従ってください。`anthropics/claude-plugins-official`へのPull Requestは受け付けられていないため、提出はポータルから行います。

## 提出前の確認

1. `main`を最新にし、ユニットテストを通す。`tests/test_plugin.py`がディレクトリの主なブロック条件（名前、README、ライセンス、ファイル数・サイズ、禁止ファイル、skillのfront matter）を確認します。

   ```text
   python -m unittest discover -s tests -v
   ```

2. Claude Codeの検証コマンドを実行する。

   ```text
   claude plugin validate .
   ```

3. ローカルで導入して動作を確かめる。

   ```text
   claude --plugin-dir .
   ```

4. リリースごとに`.claude-plugin/plugin.json`の`version`を上げる。`name`（`postgresql-skills`）は公開後に変更できない識別子です。表示名を変える場合は`displayName`を変更します。

## 提出フォームへの入力

提出には有料プラン（Pro、Max、Team、Enterpriseなど）のアカウントが必要です。Team・Enterpriseでは組織のOwner（EnterpriseではDirectory権限を持つロールも可）が提出します。

[claude.ai/directory/manage](https://claude.ai/directory/manage)で **Submit new** → **Plugin bundle** を選びます。

| 手順 | 入力 |
| --- | --- |
| Source | Repository: `soudai/postgresql-use-improve-agents-skill`。Plugin path: 空欄（ルート）。Branch or tag: 空欄（`main`）。**Validate**を実行する |
| Listing details | `plugin.json`とREADMEから自動で読み込まれる |
| Data handling | 下記の回答を参照 |
| Compliance | 連絡用メールアドレスを入力し、4つの確認事項に同意する |
| Review and submit | 新版の反映方法は**GitHub push webhook**を推奨。**Submit for review**で提出する |

### Data handlingの回答案

READMEの「データの扱いとネットワーク通信」と同じ内容です。

| 質問 | 回答 |
| --- | --- |
| 個人データを読み取る・保存するか | 利用者が指定したPostgreSQLのcatalogと統計（ロール名、SQL文、実行計画を含み得る）をローカルで読み取り、利用者が指定したローカルファイルにだけ書き込む。プラグイン自身は保存・送信しない |
| 宣言したコネクタ以外のサービスへデータを送るか | 送らない。外部通信は`advisories.py`がwww.postgresql.orgの公開ページ2つをHTTPSで取得するだけで、環境の情報は送信しない |
| データの保持期間 | プラグインは保持しない。出力ファイルは利用者が管理する |
| 18歳未満向けか | 対象外（データベース管理者・開発者向け） |

## 提出後

- 提出後に検証とセキュリティスキャンが走り、結果はポータルの**Versions**タブに表示されます。新規の掲載は公開前に担当者が確認します。
- 修正が必要な場合は修正を`main`へpushし、**Check for new commits**または**Resubmit for review**を選びます。
- 合格した版は**Publish**で公開します（既定ではAnthropicのレビュアーが公開します）。
- 公開後は`main`へのマージで新しい版が検出されます。`version`を上げてからマージしてください。

## 自前のマーケットプレイス

`.claude-plugin/marketplace.json`は、ディレクトリ掲載前や掲載と並行して`/plugin marketplace add soudai/postgresql-use-improve-agents-skill`で導入するためのものです。公式ディレクトリの掲載には不要ですが、既存の利用者のために残しています。
