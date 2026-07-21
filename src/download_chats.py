from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from storage_paths import CHAT_DIR, CONFIG_PATH, PROJECT_DIR



def load_chat_dir() -> Path:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            value = str(data.get("chat_data_dir", "")).strip()
            if value:
                return Path(value).expanduser()
        except Exception:
            pass
    return CHAT_DIR


def validate_url(url: str) -> tuple[bool, str]:
    lowered = url.lower()

    if "studio.youtube.com" in lowered:
        return (
            False,
            "YouTube StudioのURLは使用できません。"
            "\n公開チャンネルの「ライブ」タブURLを入力してください。",
        )

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "http または https から始まるURLを入力してください。"

    if "youtube.com" not in parsed.netloc and "youtu.be" not in parsed.netloc:
        return False, "YouTubeのURLを入力してください。"

    return True, ""


def main() -> None:
    print("VTuber Analytics チャット取得")
    print("--------------------------------")
    print("YouTubeチャンネルの「ライブ」タブURLを入力してください。")
    print()
    print("【URLの確認方法】")
    print("1. YouTubeで自分の公開チャンネルを開く")
    print("2. 「ライブ」タブを開く")
    print("3. ブラウザ上部のURLをコピーする")
    print()
    print("入力例:")
    print("https://www.youtube.com/@genk_aino_maria/streams")
    print()
    print("※ YouTube Studioの管理画面URLではありません。")
    print("※ 再生リストURLにも対応しています。")
    print()

    url = input("ライブ一覧URL: ").strip()
    if not url:
        print("URLが入力されていません。")
        sys.exit(1)

    valid, message = validate_url(url)
    if not valid:
        print()
        print("入力されたURLを利用できません。")
        print(message)
        print()
        print("正しい形式の例:")
        print("https://www.youtube.com/@チャンネルのハンドル名/streams")
        sys.exit(1)

    out_dir = load_chat_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(
        out_dir / "%(upload_date>%Y-%m-%d)s_%(id)s_%(title).120B.%(ext)s"
    )

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--write-subs",
        "--sub-langs",
        "live_chat",
        "--ignore-errors",
        "--no-overwrites",
        "--windows-filenames",
        "--output",
        output_template,
        url,
    ]

    print()
    print(f"保存先: {out_dir}")
    print("ライブチャットを取得しています。配信数によって時間がかかります。")
    result = subprocess.run(cmd, check=False)

    print()
    if result.returncode == 0:
        print("チャット取得処理が終了しました。")
        print("次は 3_データ更新.bat を実行してください。")
    else:
        print("一部または全部のチャット取得に失敗しました。")
        print("URLとインターネット接続を確認してください。")
        print("必要に応じて次のコマンドで yt-dlp を更新してください。")
        print("py -m pip install -U yt-dlp")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
