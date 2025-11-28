#!/usr/bin/env python3
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime
import requests

# ===== 設定 =====
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]

FROM_ADDR = GMAIL_USER
TO_ADDR   = os.environ["TO_USER"]  # とりあえず自分宛

# ★ テスト用のキーワード（ここを書き換えて試す）
CROSSREF_QUERY = "organic electrochemical transistors"

# Crossref API endpoint
CROSSREF_API_URL = "https://api.crossref.org/works"
# =================


def search_top_paper(query: str):
    """
    Crossref でキーワード検索して、一番上の論文情報を返す。
    見つからなければ None を返す。
    """
    params = {
        "query": query,
        "rows": 1,
        "sort": "relevance",
    }

    # Crossref の推奨に従って、連絡先入り User-Agent を付ける
    headers = {
        "User-Agent": f"paperbot/0.1 (mailto:{GMAIL_USER})"
    }

    try:
        resp = requests.get(CROSSREF_API_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        # 通信エラーなどの場合は None で返す
        return None, f"Request error: {e}"

    try:
        data = resp.json()
    except Exception as e:
        return None, f"JSON parse error: {e}"

    items = data.get("message", {}).get("items", [])
    if not items:
        return None, "No items found."

    item = items[0]

    title_list = item.get("title", [])
    title = title_list[0] if title_list else "(no title)"

    doi = item.get("DOI", "(no DOI)")
    url = item.get("URL", f"https://doi.org/{doi}" if doi != "(no DOI)" else "(no URL)")

    # 著者
    authors_raw = item.get("author", [])
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
        if key in item:
            parts = item[key].get("date-parts", [])
            if parts and parts[0]:
                year = str(parts[0][0])
                break

    # ジャーナル名
    container = item.get("container-title", [])
    journal = container[0] if container else "(no journal)"

    # まとめて dict で返す
    paper_info = {
        "title": title,
        "doi": doi,
        "url": url,
        "authors": authors_str,
        "year": year,
        "journal": journal,
        "query": query,
    }
    return paper_info, None


def main():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    # Crossref 検索
    paper, err = search_top_paper(CROSSREF_QUERY)

    if paper is None:
        subject = f"Paperbot Crossref test - ERROR - {now_str}"
        body = (
            f"Crossref search failed for query: '{CROSSREF_QUERY}'\n"
            f"Time: {now_str}\n"
            f"Error: {err}\n"
        )
    else:
        subject = f"Paperbot Crossref test - {now_str}"

        body = (
            f"Crossref test result for query: \"{paper['query']}\"\n"
            f"Time: {now_str}\n"
            "\n"
            f"Title  : {paper['title']}\n"
            f"Authors: {paper['authors']}\n"
            f"Journal: {paper['journal']}\n"
            f"Year   : {paper['year']}\n"
            f"DOI    : {paper['doi']}\n"
            f"URL    : {paper['url']}\n"
        )

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
