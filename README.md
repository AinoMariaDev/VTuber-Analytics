# VTuber Analytics

**Powered by Aino Maria**

YouTubeライブチャットをもとに、配信とコミュニティの傾向を分析するローカルWebアプリです。

## 主な機能
- リスナー検索
- 参加配信数・参加率・総コメント数の集計
- ファンスコア
- 新規・継続中・復帰・休眠候補の分類
- 月別参加推移
- 配信ごとの参加人数・コメント数分析
- 企画別・曜日別分析
- SQLiteによる差分保存
- ローカルWebアプリ

## バージョン
`0.6.0`

## 動作環境
- Windows 10 / 11
- Python 3.11以降
- yt-dlp
- Chrome / Edge など

## 基本操作
1. ライブチャットを取得
2. `4_データベース構築.bat` を実行
3. `9_Webアプリ起動.bat` を実行
4. `http://127.0.0.1:8765` を開く

## プライバシー
SQLiteデータベース、チャットJSON、集計CSV、ローカルレポートはGitHubへ保存しません。

## ライセンス
MIT License

## Credits
Created and tested with real streaming data.

**Powered by Aino Maria**
