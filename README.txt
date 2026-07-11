VTuber Analytics - 愛野まりあ版 v0.1

この版でできること
・既存のライブチャットJSONをSQLiteへ保存
・配信、リスナー、コメントを重複なく管理
・再実行時は新しいデータだけ追加
・常連、新規、休眠候補、配信ごとの数値をCSV出力
・データベースの状態確認

導入方法
1. このフォルダを、現在使っている
   YouTubeライブチャット一括取得セット
   のフォルダ内へ置いてください。

2. フォルダ構成が次のようになっていればOKです。

   YouTubeライブチャット一括取得セット
   ├ youtube_chat_data
   ├ youtube_chat_results
   └ VTuberAnalytics_AinoMaria_v0.1

3. 最初に
   4_データベース構築.bat
   をダブルクリックします。

4. 次に
   5_データベース分析.bat
   をダブルクリックします。

作成されるもの
・data/vtuber_analytics.db
・reports/リスナー一覧.csv
・reports/配信一覧.csv
・reports/30日以上コメントなし.csv
・reports/最新配信レポート.txt

今後の更新
1_チャット一括取得.bat
2_リスナー集計.bat
4_データベース構築.bat
5_データベース分析.bat

注意
・視聴者全体ではなく、ライブチャットにコメントした人が対象です。
・YouTubeチャンネルIDを同一人物判定の主キーに使います。
・表示名変更は別人扱いせず、最新の表示名を保存します。
