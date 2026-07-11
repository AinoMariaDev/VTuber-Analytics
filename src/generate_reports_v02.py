from __future__ import annotations

import csv
import html
from collections import Counter
from pathlib import Path

from common import REPORT_DIR, connect, load_config

def write_csv(path: Path, headers: list[str], rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

def pct(value: float) -> str:
    return f"{value * 100:.1f}%"

def main() -> None:
    config = load_config()
    owner_ids = tuple(config.get("owner_channel_ids", []))
    placeholders = ",".join("?" for _ in owner_ids) or "''"
    REPORT_DIR.mkdir(exist_ok=True)

    with connect() as conn:
        latest_date = conn.execute(
            "SELECT MAX(stream_date) AS d FROM streams"
        ).fetchone()["d"]

        total_streams = conn.execute(
            "SELECT COUNT(*) AS c FROM streams"
        ).fetchone()["c"]

        total_listeners = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM listeners
            WHERE channel_id NOT IN ({placeholders})
            """,
            owner_ids,
        ).fetchone()["c"]

        total_comments = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM messages
            WHERE channel_id NOT IN ({placeholders})
            """,
            owner_ids,
        ).fetchone()["c"]

        category_rows = conn.execute(
            f"""
            SELECT
                COALESCE(s.category, 'その他') AS category,
                COUNT(DISTINCT s.video_id) AS stream_count,
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments,
                MAX(x.participants) AS max_participants,
                MAX(x.comments) AS max_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({placeholders}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({placeholders}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            GROUP BY COALESCE(s.category, 'その他')
            ORDER BY avg_participants DESC, avg_comments DESC
            """,
            owner_ids * 2,
        ).fetchall()

        write_csv(
            REPORT_DIR / "企画別分析.csv",
            ["企画", "配信数", "平均コメント参加人数", "平均コメント数", "最高参加人数", "最高コメント数"],
            [
                (
                    r["category"], r["stream_count"], r["avg_participants"],
                    r["avg_comments"], r["max_participants"], r["max_comments"]
                )
                for r in category_rows
            ],
        )

        weekday_order = ["月", "火", "水", "木", "金", "土", "日"]
        weekday_rows_raw = conn.execute(
            f"""
            SELECT
                COALESCE(s.weekday, '') AS weekday,
                COUNT(DISTINCT s.video_id) AS stream_count,
                ROUND(AVG(x.participants), 1) AS avg_participants,
                ROUND(AVG(x.comments), 1) AS avg_comments
            FROM streams s
            LEFT JOIN (
                SELECT
                    video_id,
                    COUNT(DISTINCT CASE
                        WHEN channel_id NOT IN ({placeholders}) THEN channel_id
                    END) AS participants,
                    COUNT(CASE
                        WHEN channel_id NOT IN ({placeholders}) THEN message_id
                    END) AS comments
                FROM messages
                GROUP BY video_id
            ) x ON x.video_id = s.video_id
            GROUP BY s.weekday
            """,
            owner_ids * 2,
        ).fetchall()

        weekday_map = {r["weekday"]: r for r in weekday_rows_raw}
        weekday_rows = [weekday_map[d] for d in weekday_order if d in weekday_map]

        write_csv(
            REPORT_DIR / "曜日別分析.csv",
            ["曜日", "配信数", "平均コメント参加人数", "平均コメント数"],
            [
                (r["weekday"], r["stream_count"], r["avg_participants"], r["avg_comments"])
                for r in weekday_rows
            ],
        )

        listener_rows = conn.execute(
            f"""
            SELECT
                l.latest_display_name,
                l.channel_id,
                COUNT(DISTINCT m.video_id) AS stream_count,
                COUNT(m.message_id) AS comment_count,
                ROUND(
                    CAST(COUNT(m.message_id) AS REAL) /
                    NULLIF(COUNT(DISTINCT m.video_id), 0), 1
                ) AS avg_comments,
                l.first_seen_date,
                l.last_seen_date,
                CAST(julianday(?) - julianday(l.last_seen_date) AS INTEGER) AS days_absent
            FROM listeners l
            JOIN messages m ON m.channel_id = l.channel_id
            WHERE l.channel_id NOT IN ({placeholders})
            GROUP BY l.channel_id
            ORDER BY stream_count DESC, comment_count DESC
            """,
            (latest_date, *owner_ids),
        ).fetchall()

        listener_cards = []
        for rank, r in enumerate(listener_rows, 1):
            participation_rate = (r["stream_count"] / total_streams) if total_streams else 0
            score = round(
                min(100,
                    participation_rate * 55
                    + min((r["avg_comments"] or 0) / 35, 1) * 25
                    + (20 if (r["days_absent"] or 0) <= 14 else
                       14 if (r["days_absent"] or 0) <= 30 else
                       6 if (r["days_absent"] or 0) <= 60 else 0)
                )
            )
            listener_cards.append((
                rank,
                r["latest_display_name"],
                r["channel_id"],
                r["stream_count"],
                participation_rate,
                r["comment_count"],
                r["avg_comments"],
                r["first_seen_date"],
                r["last_seen_date"],
                r["days_absent"],
                score,
            ))

        write_csv(
            REPORT_DIR / "リスナーカルテ.csv",
            [
                "順位", "表示名", "チャンネルID", "参加配信数", "参加率",
                "総コメント数", "平均コメント数", "初参加日", "最終参加日",
                "最終配信からの日数", "スコア"
            ],
            [
                (
                    r[0], r[1], r[2], r[3], pct(r[4]), r[5], r[6],
                    r[7], r[8], r[9], r[10]
                )
                for r in listener_cards
            ],
        )

        latest_stream = conn.execute(
            """
            SELECT video_id, stream_date, title, category
            FROM streams
            WHERE stream_date IS NOT NULL
            ORDER BY stream_date DESC, imported_at DESC
            LIMIT 1
            """
        ).fetchone()

        top10 = listener_cards[:10]
        inactive_30 = sum(1 for r in listener_cards if r[9] is not None and r[9] >= 30)

        best_category = category_rows[0]["category"] if category_rows else "データなし"
        best_weekday = weekday_rows[0]["weekday"] if weekday_rows else "データなし"

        category_html = "".join(
            f"<tr><td>{html.escape(str(r['category']))}</td>"
            f"<td>{r['stream_count']}</td>"
            f"<td>{r['avg_participants']}</td>"
            f"<td>{r['avg_comments']}</td></tr>"
            for r in category_rows
        )

        top_html = "".join(
            f"<tr><td>{r[0]}</td><td>{html.escape(r[1])}</td>"
            f"<td>{r[3]}</td><td>{pct(r[4])}</td>"
            f"<td>{r[5]}</td><td>{r[10]}</td></tr>"
            for r in top10
        )

        weekday_html = "".join(
            f"<tr><td>{r['weekday']}</td><td>{r['stream_count']}</td>"
            f"<td>{r['avg_participants']}</td><td>{r['avg_comments']}</td></tr>"
            for r in weekday_rows
        )

        latest_title = html.escape(latest_stream["title"]) if latest_stream else "データなし"
        latest_category = html.escape(latest_stream["category"] or "その他") if latest_stream else "データなし"

        dashboard = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>愛野まりあ YouTube Analytics</title>
<style>
body {{
  margin: 0;
  font-family: "Yu Gothic", "Meiryo", sans-serif;
  background: #f3f5f8;
  color: #263238;
}}
header {{
  background: #24324a;
  color: white;
  padding: 24px 32px;
}}
main {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}}
.card {{
  background: white;
  border-radius: 12px;
  padding: 18px;
  box-shadow: 0 2px 12px rgba(0,0,0,.08);
}}
.kpi {{
  font-size: 32px;
  font-weight: bold;
  margin-top: 8px;
}}
.two {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-top: 20px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}}
th, td {{
  padding: 9px;
  border-bottom: 1px solid #dfe4ea;
  text-align: left;
}}
th {{
  background: #e8edf5;
}}
.note {{
  border-left: 5px solid #e97c9d;
  margin-top: 20px;
}}
@media (max-width: 800px) {{
  .grid, .two {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<header>
<h1>愛野まりあ YouTube Analytics</h1>
<div>コメント参加者ベース｜基準日 {latest_date}</div>
</header>
<main>
<section class="grid">
  <div class="card"><div>配信数</div><div class="kpi">{total_streams}</div></div>
  <div class="card"><div>リスナー数</div><div class="kpi">{total_listeners}</div></div>
  <div class="card"><div>総コメント数</div><div class="kpi">{total_comments:,}</div></div>
  <div class="card"><div>30日以上コメントなし</div><div class="kpi">{inactive_30}</div></div>
</section>

<section class="card note">
<h2>現時点の自動所見</h2>
<p>平均コメント参加人数が最も高い企画は <strong>{html.escape(str(best_category))}</strong> です。</p>
<p>曜日別では <strong>{html.escape(str(best_weekday))}曜日</strong> の数値が最上位です。</p>
<p>最新配信は「{latest_title}」、分類は <strong>{latest_category}</strong> です。</p>
<p>この所見は相関の表示であり、原因を断定するものではありません。</p>
</section>

<section class="two">
<div class="card">
<h2>常連TOP10</h2>
<table>
<thead><tr><th>順位</th><th>表示名</th><th>参加配信</th><th>参加率</th><th>コメント</th><th>スコア</th></tr></thead>
<tbody>{top_html}</tbody>
</table>
</div>
<div class="card">
<h2>企画別分析</h2>
<table>
<thead><tr><th>企画</th><th>配信数</th><th>平均参加人数</th><th>平均コメント</th></tr></thead>
<tbody>{category_html}</tbody>
</table>
</div>
</section>

<section class="card" style="margin-top:20px">
<h2>曜日別分析</h2>
<table>
<thead><tr><th>曜日</th><th>配信数</th><th>平均参加人数</th><th>平均コメント</th></tr></thead>
<tbody>{weekday_html}</tbody>
</table>
</section>
</main>
</body>
</html>"""

        (REPORT_DIR / "dashboard.html").write_text(dashboard, encoding="utf-8")

    print("v0.2レポートを作成しました。")
    print("・企画別分析.csv")
    print("・曜日別分析.csv")
    print("・リスナーカルテ.csv")
    print("・dashboard.html")

if __name__ == "__main__":
    main()
