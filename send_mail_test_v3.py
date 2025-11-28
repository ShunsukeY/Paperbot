#!/usr/bin/env python3
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime
import requests


# ======================
#  環境変数から設定取得
# ======================
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]
TO_ADDR = os.environ.get("TO_USER", GMAIL_USER)

FROM_ADDR = GMAIL_USER

# ======================
#  Crossref 検索設定
# ======================
CROSSREF_QUERY = "organic electrochemical transistors"  # ★テスト用キーワード
CROSSREF_API_URL = "https://api.crossref.org/works"
CROSSREF_ROWS = 10              # いったん10件とって、その中から上位2件を選ぶ
YEAR_FILTER_FROM = "2010-01-01" # 2010年以降に限定
TOP_N = 2                       # ← 上位何件送るか
# ======================


def score_item(item, query: str) -> int:
    """
    タイトルとクエリのマッチ度をざっくりスコア化する関数。
      - クエリ全文がタイトルに含まれれば +2
      - クエリをスペース区切りした各単語がタイトルに含まれれば +1 ずつ
    """
    title_list = item.get("title", [])
    title = title_list[0] if title_list else ""
    t = title.lower()
    q = query.lower()

    score = 0

    # クエリ全文が含まれるなら +2
    if q in t:
        score += 2

    # クエリをスペースで分けて、各単語がタイトルに含まれていたら +1
    for w in q.split():
        w = w.strip()
        if w and w in t:
            score += 1

    return score


def choose_top_items(items, query: str, n: int):
    """
    Crossref から返ってきた items から、score_item() に基づいて
    上位 n 件を返す（score が高い順）。
    """
    scored = []
    for idx, it in enumerate(items):
        s = score_item(it, query)
        # idx を入れておくと「同点なら元の順」にできる
        scored.append((s, idx, it))

    # スコア降順、同点なら元の順でソート
    scored.sort(key=lambda x: (-x[0], x[1]))

    top_items = [t[2] for t in scored[:n] if t[2] is not None]
    return top_items


def search_top_papers(query: str, top_n: int):
    """
    Crossref でタイトル検索して、上位 top_n 件の論文情報を返す。
    成功時: (papers_list, None)
    失敗時: (None, error_message)
    papers_list は paper_info dict のリスト。
    """
    params = {
        "query.title": query,
        "rows": CROSSREF_ROWS,
        "sort": "relevance",
        "filter": f"type:journal-article,from-pub-date:{YEAR_FILTER_FROM}",
    }

    headers = {
        # Crossref推奨：連絡先入りUser-Agent
        "User-Agent": f"paperbot/0.1 (mailto:{GMAIL_USER})"
    }

    try:
        resp = requests.get(CROSSREF_API_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        return None, f"Request error: {e}"

    try:
        data = resp.json()
    except Exception as e:
        return None, f"JSON parse error: {e}"

    items = data.get("message", {}).get("items", [])
    if not items:
        return None, "No items found."

    # 上位 top_n 件を選ぶ
    selected_items = choose_top_items(items, query, top_n)
    if not selected_items:
        return None, "No suitable items selected."

    papers = []
    for it in selected_items:
        # タイトル
        title_list = it.get("title", [])
        title = title_list[0] if title_list else "(no title)"

        # DOI / URL
        doi = it.get("DOI", "(no DOI)")
        url = it.get("URL", f"https://doi.org/{doi}" if doi != "(no DOI)" else "(no URL)")

        # 著者
        authors_raw = it.get("author", [])
        authors = []
        for a in authors_raw:
            given = a.get("given", "")
            family = a.get("family", "")
            name = (given + " " + family).strip()
            if not name:
                name = "(no name)"
            authors.append(name)
        authors_str = ", ".join(authors) if authors else "(no authors)"

        # 出版年
        year = "(n.d.)"
        for key in ["published-print", "published-online", "issued"]:
            if key in it:
                parts = it[key].get("date-parts", [])
                if parts and parts[0]:
                    year = str(parts[0][0])
                    break

        # ジャーナル名
        container = it.get("container-title", [])
        journal = container[0] if container else "(no journal)"

        papers.append({
            "title": title,
            "doi": doi,
            "url": url,
            "authors": authors_str,
            "year": year,
            "journal": journal,
            "query": query,
        })

    return papers, None


def main():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    papers, err = search_top_papers(CROSSREF_QUERY, TOP_N)

    if papers is None:
        subject = f"Paperbot Crossref ERROR - {now_str}"
        body = (
            f"Crossref search failed.\n"
            f"Query : \"{CROSSREF_QUERY}\"\n"
            f"Time  : {now_str}\n"
            f"Error : {err}\n"
        )
    else:
        subject = f"Paperbot Crossref (top {len(papers)}) - {now_str}"

        lines = [
            f"Crossref top {len(papers)} papers for query: \"{CROSSREF_QUERY}\"",
            f"Time: {now_str}",
            "",
        ]

        for i, p in enumerate(papers, start=1):
            lines.append(f"[{i}] {p['title']}")
            lines.append(f"    Authors: {p['authors']}")
            lines.append(f"    Journal: {p['journal']}")
            lines.append(f"    Year   : {p['year']}")
            lines.append(f"    DOI    : {p['doi']}")
            lines.append(f"    URL    : {p['url']}")
            lines.append("")  # 空行で区切る

        body = "\n".join(lines)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        smtp.send_message(msg)


if __name__ == "__main__":
    main()
