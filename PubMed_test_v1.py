#!/usr/bin/env python3
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime
import re
import requests

# ======================
#  環境変数から設定取得
# ======================
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]
TO_ADDR = os.environ.get("TO_USER", GMAIL_USER)

FROM_ADDR = GMAIL_USER

# ======================
#  PubMed 検索設定
# ======================
SEARCH_QUERY = "organic electrochemical transistors"  # ★テスト用キーワード
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ESEARCH_URL = EUTILS_BASE + "esearch.fcgi"
ESUMMARY_URL = EUTILS_BASE + "esummary.fcgi"

PUBMED_RETMAX = 10             # ESearch でいったん何件 PMIDs を取るか
YEAR_FILTER_FROM = "2010/01/01"  # 2010年以降 (pdat)
TOP_N = 2                      # ← 上位何件送るか
TOOL_NAME = "paperbot"
# ======================


def score_title(title: str, query: str) -> int:
    """
    タイトルとクエリのマッチ度をざっくりスコア化する関数。
      - クエリ全文がタイトルに含まれれば +2
      - クエリをスペース区切りした各単語がタイトルに含まれれば +1 ずつ
    Crossref版の score_item() と同じロジック。
    """
    t = title.lower()
    q = query.lower()

    score = 0

    if q in t:
        score += 2

    for w in q.split():
        w = w.strip()
        if w and w in t:
            score += 1

    return score


def choose_top_docs(docs, query: str, n: int):
    """
    docs: {pmid: doc_dict} の dict から、タイトルに対するスコアで上位n件を返す。
    （同点なら元の順に近い順で）
    """
    scored = []
    for idx, (pmid, doc) in enumerate(docs.items()):
        title = doc.get("title", "")
        s = score_title(title, query)
        scored.append((s, idx, pmid, doc))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:n]
    return [(pmid, doc) for (_, _, pmid, doc) in top if doc is not None]


def extract_year_from_pubdate(pubdate: str) -> str:
    """
    PubMed の pubdate (例: '2024 Jan 15', '2019', '2018 Dec') から
    年(4桁)だけを抽出する簡易関数。
    """
    if not pubdate:
        return "(n.d.)"
    m = re.search(r"\b(\d{4})\b", pubdate)
    if m:
        return m.group(1)
    return "(n.d.)"


def search_top_papers_pubmed(query: str, top_n: int):
    """
    PubMed E-utilities (ESearch + ESummary) を使って、
    タイトル/アブストラクトで query を検索し、上位 top_n 件の論文情報を返す。
    成功時: (papers_list, None)
    失敗時: (None, error_message)
    papers_list は paper_info dict のリスト。
    """
    # ---------- ESearch: PMID リスト取得 ----------
    esearch_params = {
        "db": "pubmed",
        # タイトル＋アブストラクトに対して検索
        "term": f"{query}[Title/Abstract]",
        "retmode": "json",
        "retmax": PUBMED_RETMAX,
        "sort": "relevance",
        "datetype": "pdat",
        "mindate": YEAR_FILTER_FROM,  # 2010/01/01 以降
        "tool": TOOL_NAME,
        "email": GMAIL_USER,
    }

    try:
        r = requests.get(ESEARCH_URL, params=esearch_params, timeout=10)
        r.raise_for_status()
    except Exception as e:
        return None, f"PubMed ESearch error: {e}"

    try:
        es_data = r.json()
    except Exception as e:
        return None, f"PubMed ESearch JSON parse error: {e}"

    idlist = es_data.get("esearchresult", {}).get("idlist", [])
    if not idlist:
        return None, "PubMed: No PMIDs found."

    # ---------- ESummary: 論文メタデータ取得 ----------
    esummary_params = {
        "db": "pubmed",
        "id": ",".join(idlist),
        "retmode": "json",
        "tool": TOOL_NAME,
        "email": GMAIL_USER,
    }

    try:
        r2 = requests.get(ESUMMARY_URL, params=esummary_params, timeout=10)
        r2.raise_for_status()
    except Exception as e:
        return None, f"PubMed ESummary error: {e}"

    try:
        sum_data = r2.json()
    except Exception as e:
        return None, f"PubMed ESummary JSON parse error: {e}"

    result = sum_data.get("result", {})
    uids = result.get("uids", [])
    if not uids:
        return None, "PubMed: No summaries returned."

    # uids の順番を保った dict を構成しておく
    docs = {}
    for pmid in uids:
        doc = result.get(pmid)
        if doc:
            docs[pmid] = doc

    # スコア上位 top_n 件を選択
    top_docs = choose_top_docs(docs, query, top_n)
    if not top_docs:
        return None, "PubMed: No suitable docs selected."

    papers = []
    for pmid, doc in top_docs:
        title = doc.get("title") or "(no title)"

        # 著者
        authors_raw = doc.get("authors", [])
        authors = []
        for a in authors_raw:
            name = a.get("name")
            if name:
                authors.append(name)
        authors_str = ", ".join(authors) if authors else "(no authors)"

        # ジャーナル名
        journal = (
            doc.get("fulljournalname")
            or doc.get("source")
            or "(no journal)"
        )

        # 年
        pubdate = doc.get("pubdate") or doc.get("epubdate") or ""
        year = extract_year_from_pubdate(pubdate)

        # DOI
        doi = "(no DOI)"
        for aid in doc.get("articleids", []):
            if aid.get("idtype") == "doi":
                val = aid.get("value")
                if val:
                    doi = val
                    break

        # URL
        if doi != "(no DOI)":
            url = f"https://doi.org/{doi}"
        else:
            # DOI がなければ PubMed ページへのリンク
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        papers.append(
            {
                "title": title,
                "doi": doi,
                "url": url,
                "authors": authors_str,
                "year": year,
                "journal": journal,
                "query": query,
                "pmid": pmid,
            }
        )

    return papers, None


def main():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    papers, err = search_top_papers_pubmed(SEARCH_QUERY, TOP_N)

    if papers is None:
        subject = f"Paperbot PubMed ERROR - {now_str}"
        body = (
            f"PubMed search failed.\n"
            f"Query : \"{SEARCH_QUERY}\"\n"
            f"Time  : {now_str}\n"
            f"Error : {err}\n"
        )
    else:
        subject = f"Paperbot PubMed (top {len(papers)}) - {now_str}"

        lines = [
            f"PubMed top {len(papers)} papers for query: \"{SEARCH_QUERY}\"",
            f"Time: {now_str}",
            "",
        ]

        for i, p in enumerate(papers, start=1):
            lines.append(f"[{i}] {p['title']}")
            lines.append(f"    Authors: {p['authors']}")
            lines.append(f"    Journal: {p['journal']}")
            lines.append(f"    Year   : {p['year']}")
            lines.append(f"    DOI    : {p['doi']}")
            lines.append(f"    PMID   : {p['pmid']}")
            lines.append(f"    URL    : {p['url']}")
            lines.append("")

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
