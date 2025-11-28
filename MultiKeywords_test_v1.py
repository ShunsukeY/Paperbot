#!/usr/bin/env python3
import os
import smtplib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html import escape

import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate

# ======================
#  ロギング設定
# ======================
logging.basicConfig(
    level=logging.INFO,  # 必要に応じて DEBUG に変更
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

# DeepL API（任意）
DEEPL_AUTH_KEY = os.environ.get("DEEPL_AUTH_KEY")
DEEPL_API_URL = os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
TRANSLATION_ENABLED = bool(DEEPL_AUTH_KEY)

# ======================
#  共通設定
# ======================

# ★ここに複数キーワードを並べる
SEARCH_QUERIES = [
    "organic electrochemical transistors",
    "organic mixed ionic-electronic conductors",
    # "mixed ionic electronic conductor sensor",
]

TOP_N = 3                   # 各クエリごとにメールに載せる件数
YEAR_FROM_CROSSREF = "2010-01-01"
YEAR_FROM_PUBMED = "2010/01/01"

CROSSREF_ROWS = 20          # Crossref から最大何件とってくるか
PUBMED_RETMAX = 20          # PubMed から最大何件とってくるか

# Crossref
CROSSREF_API_URL = "https://api.crossref.org/works"

# PubMed
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ESEARCH_URL = EUTILS_BASE + "esearch.fcgi"
ESUMMARY_URL = EUTILS_BASE + "esummary.fcgi"
EFETCH_URL = EUTILS_BASE + "efetch.fcgi"
TOOL_NAME = "paperbot"

# メールに Abstract を載せるかどうか
INCLUDE_ABSTRACT_IN_MAIL = True
# メール本文で Abstract を何文字までにするか（None で無制限）
ABSTRACT_CHAR_LIMIT = 500

# ======================
#  共通スコア・ユーティリティ
# ======================

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
    """年をソート用に int に変換 (n.d. → 0)。"""
    try:
        return int(year_str)
    except Exception:
        return 0


# ======================
#  Crossref 側
# ======================

def fetch_papers_crossref(query: str, rows: int):
    """
    Crossref から rows 件まで論文を取ってきて、共通フォーマットの dict リストで返す。
    各論文は以下のキーを持つ:
      title, doi, url, authors, year, journal, query, source,
      pmid, abstract, abstract_ja, pub_types
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

        # Crossref の type
        crossref_type = it.get("type") or "(no type)"

        # abstract（JATSタグ付きのことが多いのでざっくりタグ削除）
        raw_abs = it.get("abstract")
        if raw_abs:
            abs_text = re.sub(r"<[^>]+>", "", raw_abs).strip()
        else:
            abs_text = None

        papers.append({
            "title": title,
            "doi": doi,
            "url": url,
            "authors": authors_str,
            "year": year,
            "journal": journal,
            "query": query,
            "source": "Crossref",
            "pmid": None,
            "abstract": abs_text,
            "abstract_ja": None,
            "pub_types": [crossref_type],  # Crossref の type を保持
        })

    logger.info("Crossref: normalized %d papers", len(papers))
    return papers, None


# ======================
#  PubMed 側
# ======================

def extract_year_from_pubdate(pubdate: str) -> str:
    """PubMed pubdate から年(4桁)だけ抽出。"""
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
    abstract はここでは取得せず、あとで TOP_N だけ EFetch でとる。
    """
    logger.info("PubMed: query=%r retmax=%d", query, retmax)

    # ---------- ESearch ----------
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

    # ---------- ESummary ----------
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
                "abstract": None,      # 後で EFetch
                "abstract_ja": None,
                "pub_types": None,     # 後で EFetch
            }
        )

    logger.info("PubMed: normalized %d papers", len(papers))
    return papers, None


# ======================
#  PubMed Abstract & PublicationType 取得 (EFetch)
# ======================

def fetch_pubmed_abstracts(pmids):
    """
    指定PMID群の abstract と PublicationType を EFetch でまとめて取得して dict にして返す。
    戻り値: {pmid: {"abstract": str, "pub_types": [str, ...]}}
    """
    if not pmids:
        return {}

    logger.info("Fetching PubMed abstracts/types for %d PMIDs", len(pmids))

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": GMAIL_USER,
    }

    try:
        r = requests.get(EFETCH_URL, params=params, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error("PubMed EFetch failed: %s", e)
        return {}

    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        logger.error("EFetch XML parse error: %s", e)
        return {}

    info_map = {}
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pmid = pmid_el.text.strip()

        # abstract
        texts = []
        for a in article.findall(".//AbstractText"):
            texts.append("".join(a.itertext()))
        abstract = "\n".join(texts).strip() if texts else ""

        # publication types
        pub_types = []
        for pt in article.findall(".//PublicationTypeList/PublicationType"):
            if pt.text:
                pub_types.append(pt.text.strip())

        info_map[pmid] = {
            "abstract": abstract or "(no abstract)",
            "pub_types": pub_types,
        }

    logger.info("Fetched %d abstracts/types from EFetch", len(info_map))
    return info_map


def add_abstracts_to_papers(papers):
    """
    ranked（TOP_N）論文リストのうち、PubMed ソースのものだけ EFetch して
    paper["abstract"] と paper["pub_types"] を埋めて返す。
    """
    pmids = sorted({
        p["pmid"]
        for p in papers
        if p.get("pmid") and "PubMed" in p.get("source", "")
    })

    if not pmids:
        logger.info("No PubMed papers in TOP_N, skip EFetch")
        return papers

    info_map = fetch_pubmed_abstracts(pmids)

    updated = []
    for p in papers:
        pmid = p.get("pmid")
        if pmid and pmid in info_map:
            info = info_map[pmid]
            q = dict(p)

            # abstract: PubMed のものを優先
            abs_text = info.get("abstract")
            if abs_text:
                q["abstract"] = abs_text

            # pub_types: Crossref と統合
            new_types = info.get("pub_types") or []
            old_types = q.get("pub_types") or []
            if new_types or old_types:
                merged_types = list(dict.fromkeys(old_types + new_types))
            else:
                merged_types = None
            q["pub_types"] = merged_types

            updated.append(q)
        else:
            updated.append(p)

    return updated


# ======================
#  種別分類（Review / Report 等）
# ======================

def guess_article_type(paper):
    """
    PubMed の PublicationTypeList や Crossref type から、ざっくり種別ラベルを決める。
    戻り値の例: "Review", "Meta-analysis", "Clinical trial", "Case report", "Editorial", ...
    """
    pub_types = paper.get("pub_types") or []
    pts_lower = [pt.lower() for pt in pub_types]

    if any("meta-analysis" in pt for pt in pts_lower):
        return "Meta-analysis"
    if any("systematic review" in pt for pt in pts_lower):
        return "Systematic review"
    if any("review" in pt for pt in pts_lower):
        return "Review"
    if any("randomized controlled trial" in pt for pt in pts_lower):
        return "Randomized controlled trial"
    if any("clinical trial" in pt for pt in pts_lower):
        return "Clinical trial"
    if any("case reports" in pt for pt in pts_lower) or any("case report" in pt for pt in pts_lower):
        return "Case report"
    if any("editorial" in pt for pt in pts_lower):
        return "Editorial"
    if any("letter" in pt for pt in pts_lower):
        return "Letter"
    if any("comment" in pt for pt in pts_lower):
        return "Comment"

    if pub_types:
        return pub_types[0]

    return "(unknown)"


def annotate_article_types(papers):
    """各 paper に article_type を付与。"""
    updated = []
    for p in papers:
        q = dict(p)
        q["article_type"] = guess_article_type(q)
        updated.append(q)
    return updated


# ======================
#  マージ & スコアリング（クエリごと）
# ======================

def merge_and_score(papers_crossref, papers_pubmed, query: str, top_n: int):
    """
    Crossref / PubMed 両方の papers をマージし、
    DOI（なければタイトル＋年）でざっくり重複排除したうえで
    score_title() に基づいて上位 top_n を返す。
    """
    logger.info(
        "Merge & score (%s): Crossref=%d, PubMed=%d, top_n=%d",
        query, len(papers_crossref), len(papers_pubmed), top_n
    )

    all_papers = []
    all_papers.extend(papers_crossref)
    all_papers.extend(papers_pubmed)

    if not all_papers:
        logger.warning("Merge & score (%s): No papers to merge", query)
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
            # source 統合
            if p["source"] not in existing["source"]:
                existing["source"] += "+" + p["source"]
            # pmid 補完
            if not existing.get("pmid") and p.get("pmid"):
                existing["pmid"] = p["pmid"]
            # abstract 補完
            if not existing.get("abstract") and p.get("abstract"):
                existing["abstract"] = p["abstract"]
            # pub_types 統合
            old_types = existing.get("pub_types") or []
            new_types = p.get("pub_types") or []
            if old_types or new_types:
                merged_types = list(dict.fromkeys(old_types + new_types))
            else:
                merged_types = None
            existing["pub_types"] = merged_types
        else:
            merged[key] = dict(p)

    logger.info("After dedup (%s): %d unique papers", query, len(merged))

    # スコア計算
    scored_list = []
    for p in merged.values():
        s = score_title(p["title"], query)
        scored_list.append((s, safe_year_to_int(p["year"]), p))

    scored_list.sort(key=lambda x: (-x[0], -x[1]))

    logger.info(
        "Scoring done (%s). Top %d / %d unique papers:",
        query, min(top_n, len(scored_list)), len(scored_list),
    )
    for rank, (s, year_int, p) in enumerate(scored_list, start=1):
        title_short = (p.get("title", "") or "").replace("\n", " ")
        if len(title_short) > 120:
            title_short = title_short[:117] + "..."
        logger.info(
            "  [%s] #%02d score=%d year=%s source=%s doi=%s title=%s",
            query,
            rank,
            s,
            p.get("year", ""),
            p.get("source", ""),
            p.get("doi", ""),
            title_short,
        )

    top = [t[2] for t in scored_list[:top_n]]
    logger.info("Returning top %d papers (%s)", len(top), query)
    return top


# ======================
#  和訳（DeepL API）
# ======================

def translate_abstracts_ja(papers):
    """
    paper["abstract"] を DeepL API で日本語訳し、paper["abstract_ja"] に格納。
    DEEPL_AUTH_KEY 未設定なら何もしない。
    """
    if not TRANSLATION_ENABLED:
        logger.info("DEEPL_AUTH_KEY not set; skip JA translation")
        return papers

    texts = []
    idxs = []
    for idx, p in enumerate(papers):
        text = p.get("abstract")
        if text:
            texts.append(text)
            idxs.append(idx)

    if not texts:
        logger.info("No abstracts to translate (JA)")
        return papers

    logger.info("Translating %d abstracts to JA via DeepL", len(texts))

    try:
        resp = requests.post(
            DEEPL_API_URL,
            headers={
                "Authorization": f"DeepL-Auth-Key {DEEPL_AUTH_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": texts,
                "target_lang": "JA",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("DeepL request failed: %s", e)
        return papers

    try:
        data = resp.json()
    except Exception as e:
        logger.error("DeepL JSON parse error: %s", e)
        return papers

    translations = data.get("translations", [])
    if not isinstance(translations, list):
        logger.error("DeepL unexpected response structure: %r", data)
        return papers

    if len(translations) != len(texts):
        logger.warning(
            "DeepL translations length mismatch: %d vs %d",
            len(translations), len(texts)
        )

    updated = list(papers)
    for i, idx in enumerate(idxs):
        if i >= len(translations):
            break
        ja_text = translations[i].get("text")
        if ja_text:
            updated[idx] = dict(updated[idx])
            updated[idx]["abstract_ja"] = ja_text

    return updated


# ======================
#  メイン処理（複数クエリを回してメール送信）
# ======================

def main():
    logger.info("=== paperbot Crossref+PubMed (multi-query, abstracts+types+JA, HTML mail) start ===")
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    plain_sections = []
    html_sections = []
    any_papers = False

    for query in SEARCH_QUERIES:
        logger.info("=== Query %r ===", query)

        crossref_papers, crossref_err = fetch_papers_crossref(query, CROSSREF_ROWS)
        pubmed_papers, pubmed_err = fetch_papers_pubmed(query, PUBMED_RETMAX)

        logger.info(
            "Fetch finished (%s): Crossref=%d (err=%r), PubMed=%d (err=%r)",
            query,
            len(crossref_papers), crossref_err,
            len(pubmed_papers), pubmed_err
        )

        ranked = merge_and_score(crossref_papers, pubmed_papers, query, TOP_N)
        logger.info("Ranked list size (%s) = %d", query, len(ranked))

        # TOP_N に対してのみ PubMed abstract / type を取得
        ranked = add_abstracts_to_papers(ranked)
        # 和訳
        ranked = translate_abstracts_ja(ranked)
        # 種別ラベル
        ranked = annotate_article_types(ranked)

        # ===== テキスト版セクション =====
        lines = [f'=== Query: "{query}" ===']

        if not ranked:
            lines.append("No papers found.")
            if crossref_err:
                lines.append(f"[Crossref] {crossref_err}")
            if pubmed_err:
                lines.append(f"[PubMed] {pubmed_err}")
        else:
            any_papers = True
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
                lines.append(f"    Type   : {p.get('article_type', '(unknown)')}")
                lines.append(f"    DOI    : {p['doi']}")
                lines.append(f"    Source : {p['source']}")
                lines.append(f"    URL    : {p['url']}")

                if INCLUDE_ABSTRACT_IN_MAIL and p.get("abstract"):
                    abs_en = p["abstract"]
                    if ABSTRACT_CHAR_LIMIT is not None and len(abs_en) > ABSTRACT_CHAR_LIMIT:
                        abs_en = abs_en[:ABSTRACT_CHAR_LIMIT] + "..."
                    lines.append("    Abstract (EN):")
                    for line in abs_en.splitlines():
                        lines.append("        " + line)

                if INCLUDE_ABSTRACT_IN_MAIL and p.get("abstract_ja"):
                    abs_ja = p["abstract_ja"]
                    if ABSTRACT_CHAR_LIMIT is not None and len(abs_ja) > ABSTRACT_CHAR_LIMIT:
                        abs_ja = abs_ja[:ABSTRACT_CHAR_LIMIT] + "..."
                    lines.append("    Abstract (JA):")
                    for line in abs_ja.splitlines():
                        lines.append("        " + line)

                lines.append("")

        plain_sections.append("\n".join(lines))

        # ===== HTML版セクション =====
        html_lines = [f'<h2>Query: "{escape(query)}"</h2>']

        if not ranked:
            html_lines.append("<p>No papers found.</p>")
            if crossref_err:
                html_lines.append(f"<p><i>[Crossref] {escape(crossref_err)}</i></p>")
            if pubmed_err:
                html_lines.append(f"<p><i>[PubMed] {escape(pubmed_err)}</i></p>")
        else:
            if crossref_err:
                html_lines.append(f"<p><i>[Crossref note] {escape(crossref_err)}</i></p>")
            if pubmed_err:
                html_lines.append(f"<p><i>[PubMed note] {escape(pubmed_err)}</i></p>")

            for i, p in enumerate(ranked, start=1):
                html_lines.append("<p>")
                html_lines.append(f"<b>[{i}] {escape(p['title'])}</b><br>")
                html_lines.append(f"Authors: {escape(p['authors'])}<br>")
                html_lines.append(f"Journal: {escape(p['journal'])}<br>")
                html_lines.append(f"Year: {escape(p['year'])}<br>")
                html_lines.append(f"Type: {escape(p.get('article_type', '(unknown)'))}<br>")
                html_lines.append(f"DOI: {escape(p['doi'])}<br>")
                html_lines.append(f"Source: {escape(p['source'])}<br>")
                html_lines.append(f'URL: <a href="{escape(p["url"])}">{escape(p["url"])}</a><br>')

                if INCLUDE_ABSTRACT_IN_MAIL and p.get("abstract"):
                    abs_en = p["abstract"]
                    if ABSTRACT_CHAR_LIMIT is not None and len(abs_en) > ABSTRACT_CHAR_LIMIT:
                        abs_en = abs_en[:ABSTRACT_CHAR_LIMIT] + "..."
                    html_lines.append("<br><b>Abstract (EN):</b><br>")
                    html_lines.append(
                        f'<div style="white-space: pre-wrap; font-size:90%;">{escape(abs_en)}</div>'
                    )

                if INCLUDE_ABSTRACT_IN_MAIL and p.get("abstract_ja"):
                    abs_ja = p["abstract_ja"]
                    if ABSTRACT_CHAR_LIMIT is not None and len(abs_ja) > ABSTRACT_CHAR_LIMIT:
                        abs_ja = abs_ja[:ABSTRACT_CHAR_LIMIT] + "..."
                    html_lines.append("<br><b>Abstract (JA):</b><br>")
                    html_lines.append(
                        f'<div style="white-space: pre-wrap; font-size:90%;">{escape(abs_ja)}</div>'
                    )

                html_lines.append("</p>")

        html_sections.append("\n".join(html_lines))

    # 件名
    if any_papers:
        subject = f"Paperbot Crossref+PubMed ({len(SEARCH_QUERIES)} queries) - {now_str}"
    else:
        subject = f"Paperbot Crossref+PubMed ERROR (no results) - {now_str}"

    # ===== プレーンテキスト本文 =====
    plain_body_lines = [
        f"Run time: {now_str}",
        "",
    ]
    if not TRANSLATION_ENABLED:
        plain_body_lines.append("NOTE: JA translation disabled (DEEPL_AUTH_KEY not set).")
        plain_body_lines.append("")
    plain_body_lines.append(("\n" + "-" * 60 + "\n").join(plain_sections))
    plain_body = "\n".join(plain_body_lines)

    # ===== HTML本文 =====
    html_body_parts = [
        "<html><body>",
        f"<p>Run time: {escape(now_str)}</p>",
    ]
    if not TRANSLATION_ENABLED:
        html_body_parts.append(
            '<p><i>NOTE: JA translation disabled (DEEPL_AUTH_KEY not set).</i></p>'
        )
    html_body_parts.append("<hr>")
    html_body_parts.append("<hr>".join(html_sections))
    html_body_parts.append("</body></html>")
    html_body = "\n".join(html_body_parts)

    # ===== メール組み立て（multipart/alternative） =====
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Date"] = formatdate(localtime=True)

    part_text = MIMEText(plain_body, "plain", "utf-8")
    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_text)
    msg.attach(part_html)

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
