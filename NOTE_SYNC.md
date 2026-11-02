# note RSS → Notion 山行索引

TimeTreeとは独立したPython標準ライブラリのみの処理。毎週日曜19:00 JST（cronは日曜10:00 UTC、GitHubによる遅延あり）。

## 現在の状態と切替条件

実装・検証用ブランチ。Notion実接続、書き込み読み戻し、再実行時の重複防止を確認するまで既存ChatGPTタスクを停止しない。コード／テスト成功と本番同期成功は別。

## 必要なActions secrets

Settings → Secrets and variables → Actionsで設定。

| Secret | 内容 |
| --- | --- |
| NOTION_TOKEN | 専用Notion内部インテグレーションのトークン |
| NOTION_HIKES_DATA_SOURCE_ID | 既存山行記録DBのdata source ID（DB IDではない） |
| NOTION_SYNC_LOG_PAGE_ID | 既存の外部連携・同期記録ページID |
| NOTION_NOTE_ROUTES_JSON | 任意。記事URLから既存山行ページIDへのJSON対応表 |

Notion側で対象DBと同期記録ページだけを専用接続に共有する。読み取り・本文追記が必要。ChatGPTのNotion接続とは別認証。トークン、対応表、個人Notion IDを公開コードやログへ入れない。

## 動作と制約

DB全件と子ブロック本文をページ送りして読み、note URLを重複キーに照合。追跡パラメータは除去。明示対応表がある記事は既存山行本文へ不足リンクのみ追記。対応不明／競合は既存同期記録ページへ候補として保存し再追加しない。既存プロパティ、本人メモを変更せず、削除・本文転載・日付推測を行わない。公開日を実施日へ転用しない。RSS外の全履歴は対象外。

読み取り失敗・スキーマ不一致では書き込み開始前に停止。書き込み後は本文・DB・同期ログを読み戻す。429/5xxは読み取りのみ再試行。書き込み結果不明は失敗とし、再実行は再照合から始める。公開Actionsログは件数のみ。

**従来ChatGPTタスクの文章解釈による新規山行の自動作成は未移行。** 未知の記事は候補へ留める。新規山行の登録／関連付けには内容確認が必要。同等機能への完全移行にはモデルAPI等とその評価が別途必要であり、現在は完全切替できる状態とは扱わない。

## 検証手順

1. Note sync validationで単体テストと実RSS取得を確認。
2. secretsとNotion共有を設定。
3. Note RSS to Notionをapply=falseで実行し全件読み取りを確認。
4. apply=trueで書き込み・読み戻しを検証。
5. 再実行し記事と候補の追加0件を確認。
6. 機能差の扱いを決め、Notionへ検証結果を保存してから旧タスクを停止。

mainへ未統合のブランチのscheduleは稼働しない。公開リポジトリの定期実行は長期間活動がない場合に無効化され得る。障害はActionsの失敗として確認し、通知設定を確認する。候補の原本はNotionとし、公開issueへ転載しない。

## 他処理の移行調査

- COROS公式MCPはOAuth2.0による個人接続を申請不要で提供。独立した認証とトークン更新後の保存先が必要。ChatGPTのトークンは取り出さない。公式説明ではアクセストークン30日、refresh token180日、更新成功で新refresh token発行。読み取りだけならGitHub定期実行の候補だが継続更新は未検証。
- COROS→NotionとSites再公開は別処理。Sitesの所有者限定公開を外部Actionsで維持する認証・APIは未確認。現タスクは維持。
- noteはRSS取得部分を移せる。山行対応の意味判断は上記の通り別途必要。
- エピソード補完も公開RSS等取得とNotion更新は候補だが、台本・収録の意味判断は追加実装が必要。
- 月次保守は本人判断・承認を含むため現タスク維持が妥当。
- ヤマレコ回答確認はメール用の外部認証と重複管理が必要。未実装。

公式資料：
- https://developers.notion.com/reference/query-a-data-source
- https://developers.notion.com/reference/patch-block-children
- https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP
- https://github.com/coroslab/COROS-MCP
