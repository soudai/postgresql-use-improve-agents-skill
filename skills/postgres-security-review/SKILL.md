---
name: postgres-security-review
description: Review PostgreSQL roles, row level security, authentication, TLS, deployment privileges and security updates. Use to assess exposure, propose scoped remediation, or verify security changes using catalog snapshots, access contracts and staging tests.
---

# PostgreSQLのセキュリティ診断

対象のクラスタ・DB、接続ロール、保護対象と許可する操作、管理・レプリケーション経路を確認する。取得済みの証拠があれば先に解析する。実DBは指定された接続先から読み取りで調査し、追加の特権情報は管理者による限定収集で補う。

1. [収集と入力](references/evidence.md)の形式で、観測対象、時刻、ロールと期待するアクセスを記録する。権限不足、別DB、採取上限、古い証拠を報告する。
2. `scripts/review.py`で候補を抽出し、[権限とRLS](references/access.md)、[認証と実行環境](references/authentication.md)、[更新と復旧](references/changes.md)のうち依頼に対応する項目を確認する。
3. 判定範囲、根拠、成立条件、業務への影響、変更案、検証、復旧を示す。passは確認済みの対象・操作・時点に限定し、別経路の安全性へ広げない。子のRLSフラグ、鍵のmode、SCRAM形式だけで全体を安全としない。アプリと同じ認証条件で許可・拒否を検証する。公開可能なIssue案は`--format issue`で作り、共有先を確認する。
4. 変更を求められたら、対象を限定したSQL・設定差分と管理経路・停止条件を用意する。適用は依頼で許可された範囲で行い、再診断で改善を確認する。

平文パスワード、verifier、秘密鍵、秘密入りDSNは収集や出力に含めない。診断中はcatalogに限定し、アプリ関数・policyの実行と越境試験は許可された隔離環境で行う。複数DBはDBごとに確認する。
