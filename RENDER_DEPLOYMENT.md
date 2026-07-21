# Render公開手順

1. このフォルダの中身を非公開GitHubリポジトリへ登録します。
2. RenderでNew → Blueprintを選び、そのリポジトリを指定します。
3. render.yamlがWebサービスと1GB永続ディスクを作成します。
4. 初回起動時のみ、同梱DBと基本設定が /var/data へコピーされます。
5. 公開URLで初期管理者を作成します。
6. Google Cloudに次のリダイレクトURIを追加します。
   https://あなたのRenderドメイン/api/youtube/oauth/callback
7. アプリ設定画面でも同じURI、クライアントID、シークレットを保存し、再接続します。

注意:
- リポジトリは非公開にしてください。
- OAuthシークレット、トークン、管理者パスワードはZIPに含めていません。
- SQLiteのため、サービスは1インスタンスのまま運用してください。
- /health が200なら起動正常です。
