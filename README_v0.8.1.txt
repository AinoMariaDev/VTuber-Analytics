VTuber Analytics v0.8.1 - モデレーション確認

追加:
・モデレーション専用タブ
・セクハラ、距離感、暴言候補のキーワード検知
・正規表現ルール対応
・前後3件のコメント文脈表示
・手動判定
・対応メモ
・発言者別の確認履歴
・確認済みデータのCSV出力
・ローカル専用ルールファイル

重要:
自動検知は「要確認候補」です。
セクハラ等を自動断定するものではありません。

ルール:
初回起動時に moderation_rules.local.json が自動生成されます。
このファイルはGitHubへアップロードされません。
moderation_rules.example.json を参考に編集できます。

導入:
1. ZIPを解凍
2. src / web / VERSION / .gitignore / moderation_rules.example.json を上書き
3. Webアプリを終了して再起動
4. ブラウザで Ctrl + F5
5. 「モデレーション」タブを開く

GitHub:
Summary: Release v0.8.1 moderation review tools
Description: Add candidate detection, context review, manual classification, notes, history, and CSV export.
