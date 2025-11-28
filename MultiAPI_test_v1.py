#!/usr/bin/env python3
import os
import smtplib
import logging
import re
from datetime import datetime

import requests
from email.mime.text import MIMEText
from email.utils import formatdate

# ======================
#  ロギング設定
# ======================
logging.basicConfig(
    level=logging.INFO,  # 必要なら DEBUG に
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ======================
#  環境変数から設定取得
# ======================
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]
TO_ADDR = os.environ.get("TO_USER", GMAIL_USER)
FROM_ADDR = GMAIL_USER

# ======================
#  共通設定
# ======================
SEARCH_QUERY = "organic electrochemical transistors"  # ★テスト用キーワード

TOP_N = 5                   # 最終的にメールする件数
YEAR_FROM_CROSSREF = "2010-01-01"
YEAR_FROM_PUBMED = "2010/01/01"

CROSSREF_ROWS = 20          # Crossrefから最大何件とってくるか
PUBMED_RETMAX = 20          # PubMedから最大何件とってくるか

# Crossref
CROSSREF_API_URL = "https://api.crossref.org/works"

# PubMed
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ESEARCH_URL = EUTILS_BASE + "esearch.fcgi"
ESUMMARY_URL = EUTILS_BASE + "esummary.fcgi"
TOOL_NAME = "paperbot"
# ======================


# --------------------------------------------------
#  共通スコア関数
# --------------------------------------------------
def score_title(title: str, query: str) -> int:
    """
    タイトルとクエリのマッチ度をざっくりスコア化。
      - クエリ全文がタイトルに含まれれば +2
      - クエリをスペース区切りした各単語がタイトルに含まれれば +1 ずつ
    Crossref / PubMed 両方で共通に使う。
    """
    t = (title or "").lower()
    q = query.lower()

    score = 0

    if q in t:
        score += 2

    for w in q.split():
        w = w.strip()
        if w and w in t:
            score += 1

    return score


def safe_year_to_int(year_str: str) -> int:
    """
    年をソート用に int に変換 (n.d. → 0)。
    """
    try:
        return int(year_str)
    except Exception:
        return 0


# --------------------------------------------------
#  Crossref 側
# --------------------------------------------------
def fetch_papers_crossref(query: str, rows: int):
    """
    Crossref から rows 件まで論文を取ってきて、共通フォーマットの dict リストで返す。
    ここではスコアリングはせず、「候補を集める」役割だけ。
    """
    logger.info("Crossref: query=%r rows=%d", query, rows)

    params = {
        "query": query,
        "rows": rows,
        "sort": "published",
        "order": "desc",
        "filter": f"type:journal-article,from-pub-date:{YEAR_FROM_CROSSREF}",
    }

    headers = {
        "User-Agent": f"paperbot/0.1 (mailto:{GMAIL_USER})"
    }

    try:
        resp = requests.get(CROSSREF_API_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Crossref request failed: %s", e)
        return [], f"Crossref request error: {e}"

    try:
        data = resp.json()
    except Exception as e:
        logger.error("Crossref JSON parse error: %s", e)
        return [], f"Crossref JSON parse error: {e}"

    items = data.get("message", {}).get("items", [])
    logger.info("Crossref: got %d items", len(items))

    if not items:
        return [], "Crossref: No items found."

    papers = []
    for it in items:
        title_list = it.get("title", [])
        title = title_list[0] if title_list else "(no title)"

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
            "source": "Crossref",
        })

    logger.info("Crossref: normalized %d papers", len(papers))
    return papers, None


# --------------------------------------------------
#  PubMed 側
# --------------------------------------------------
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


def fetch_papers_pubmed(query: str, retmax: int):
    """
    PubMed E-utilities (ESearch + ESummary) を使って、
    query にマッチする論文を retmax 件まで取得。
    Crossref と同じ共通フォーマットで papers を返す。
    """
    logger.info("PubMed: query=%r retmax=%d", query, retmax)

    # ---------- ESearch: PMID リスト ----------
    esearch_params = {
        "db": "pubmed",
        "term": f"{query}[Title/Abstract]",
        "retmode": "json",
        "retmax": retmax,
        "sort": "relevance",
        "datetype": "pdat",
        "mindate": YEAR_FROM_PUBMED,
        "tool": TOOL_NAME,
        "email": GMAIL_USER,
    }

    try:
        r = requests.get(ESEARCH_URL, params=esearch_params, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error("PubMed ESearch failed: %s", e)
        return [], f"PubMed ESearch error: {e}"

    try:
        es_data = r.json()
    except Exception as e:
        logger.error("PubMed ESearch JSON parse error: %s", e)
        return [], f"PubMed ESearch JSON parse error: {e}"

    idlist = es_data.get("esearchresult", {}).get("idlist", [])
    logger.info("PubMed: ESearch got %d PMIDs", len(idlist))

    if not idlist:
        return [], "PubMed: No PMIDs found."

    # ---------- ESummary: メタデータ ----------
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
        logger.error("PubMed ESummary failed: %s", e)
        return [], f"PubMed ESummary error: {e}"

    try:
        sum_data = r2.json()
    except Exception as e:
        logger.error("PubMed ESummary JSON parse error: %s", e)
        return [], f"PubMed ESummary JSON parse error: {e}"

    result = sum_data.get("result", {})
    uids = result.get("uids", [])
    if not uids:
        logger.warning("PubMed: No summaries returned (uids empty)")
        return [], "PubMed: No summaries returned."

    papers = []
    for pmid in uids:
        doc = result.get(pmid)
        if not doc:
            continue

        title = doc.get("title") or "(no title)"

        authors_raw = doc.get("authors", [])
        authors = []
        for a in authors_raw:
            name = a.get("name")
            if name:
                authors.append(name)
        authors_str = ", ".join(authors) if authors else "(no authors)"

        journal = (
            doc.get("fulljournalname")
            or doc.get("source")
            or "(no journal)"
        )

        pubdate = doc.get("pubdate") or doc.get("epubdate") or ""
        year = extract_year_from_pubdate(pubdate)

        doi = "(no DOI)"
        for aid in doc.get("articleids", []):
            if aid.get("idtype") == "doi":
                val = aid.get("value")
                if val:
                    doi = val
                    break

        if doi != "(no DOI)":
            url = f"https://doi.org/{doi}"
        else:
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
                "source": "PubMed",
            }
        )

    logger.info("PubMed: normalized %d papers", len(papers))
    return papers, None


# --------------------------------------------------
#  マージ & スコアリング
# --------------------------------------------------
def merge_and_score(papers_crossref, papers_pubmed, query: str, top_n: int):
    """
    Crossref / PubMed 両方の papers をマージし、
    DOI（なければタイトル＋年）でざっくり重複排除したうえで
    score_title() に基づいて上位 top_n を返す。
    """
    logger.info(
        "Merge & score: Crossref=%d, PubMed=%d, top_n=%d",
        len(papers_crossref), len(papers_pubmed), top_n
    )

    all_papers = []
    all_papers.extend(papers_crossref)
    all_papers.extend(papers_pubmed)

    if not all_papers:
        logger.warning("Merge & score: No papers to merge")
        return []

    # 重複排除
    merged = {}
    for p in all_papers:
        doi = p.get("doi")
        if doi and doi != "(no DOI)":
            key = doi.lower()
        else:
            key = f"{p.get('title','').lower()}_{p.get('year','')}"

        if key in merged:
            existing = merged[key]
            if p["source"] not in existing["source"]:
                existing["source"] += "+" + p["source"]
        else:
            merged[key] = dict(p)

    logger.info("After dedup: %d unique papers", len(merged))

    # スコア計算（score, year_int, paper）のリストに
    scored_list = []
    for p in merged.values():
        s = score_title(p["title"], query)
        scored_list.append((s, safe_year_to_int(p["year"]), p))

    # スコア降順 → 年の降順 でソート
    scored_list.sort(key=lambda x: (-x[0], -x[1]))

    # ★ここでスコア順リストをログに吐く
    logger.info(
        "Scoring done. Top %d / %d unique papers:",
        min(top_n, len(scored_list)),
        len(scored_list),
    )
    for rank, (s, year_int, p) in enumerate(scored_list, start=1):
        # 長すぎるタイトルはログでは切る
        title_short = (p.get("title", "") or "").replace("\n", " ")
        if len(title_short) > 120:
            title_short = title_short[:117] + "..."
        logger.info(
            "  #%02d score=%d year=%s source=%s doi=%s title=%s",
            rank,
            s,
            p.get("year", ""),
            p.get("source", ""),
            p.get("doi", ""),
            title_short,
        )
        # 全件ログると多いと思えば、TOP_NだけにしてもOK
        # if rank >= top_n:
        #     break

    top = [t[2] for t in scored_list[:top_n]]
    logger.info("Returning top %d papers", len(top))
    return top


# --------------------------------------------------
#  メイン処理（メール送信）
# --------------------------------------------------
def main():
    logger.info("=== paperbot Crossref+PubMed start ===")
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    crossref_papers, crossref_err = fetch_papers_crossref(SEARCH_QUERY, CROSSREF_ROWS)
    pubmed_papers, pubmed_err = fetch_papers_pubmed(SEARCH_QUERY, PUBMED_RETMAX)

    logger.info(
        "Fetch finished: Crossref=%d (err=%r), PubMed=%d (err=%r)",
        len(crossref_papers), crossref_err,
        len(pubmed_papers), pubmed_err
    )

    ranked = merge_and_score(crossref_papers, pubmed_papers, SEARCH_QUERY, TOP_N)
    logger.info("Ranked list size = %d", len(ranked))

    # メール本文組み立て
    if not ranked:
        subject = f"Paperbot Crossref+PubMed ERROR - {now_str}"
        lines = [
            "No papers found from Crossref or PubMed.",
            f"Query: \"{SEARCH_QUERY}\"",
            f"Time : {now_str}",
            "",
        ]
        if crossref_err:
            lines.append(f"[Crossref error] {crossref_err}")
        if pubmed_err:
            lines.append(f"[PubMed error] {pubmed_err}")
        body = "\n".join(lines)
    else:
        subject = f"Paperbot Crossref+PubMed (top {len(ranked)}) - {now_str}"

        lines = [
            f"Top {len(ranked)} papers for query: \"{SEARCH_QUERY}\"",
            f"Time: {now_str}",
            "",
        ]

        if crossref_err:
            lines.append(f"[Crossref note] {crossref_err}")
        if pubmed_err:
            lines.append(f"[PubMed note] {pubmed_err}")
        if crossref_err or pubmed_err:
            lines.append("")

        for i, p in enumerate(ranked, start=1):
            lines.append(f"[{i}] {p['title']}")
            lines.append(f"    Authors: {p['authors']}")
            lines.append(f"    Journal: {p['journal']}")
            lines.append(f"    Year   : {p['year']}")
            lines.append(f"    DOI    : {p['doi']}")
            lines.append(f"    Source : {p['source']}")
            lines.append(f"    URL    : {p['url']}")
            lines.append("")

        body = "\n".join(lines)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Date"] = formatdate(localtime=True)

    logger.info("Connecting to Gmail SMTP...")
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        smtp.send_message(msg)
    logger.info("Mail sent to %s", TO_ADDR)
    logger.info("=== paperbot Crossref+PubMed end ===")


if __name__ == "__main__":
    main()
