#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cyber Digest - RSS Collection + Article Body Extraction + Gemini Analysis

Collects security news from RSS feeds, fetches each article's body text,
and analyzes with Gemini API (plain HTTP, stdlib only).

2026-08-10 変更:
    従来はタイトルとURLだけを Gemini に渡していたため、AI が中身を推測して
    書く（ハルシネーション）余地があった。各記事の本文を取得して渡すことで
    「ソースに基づく要約」に変更した。本文が取れない記事は RSS の description に
    フォールバックし、それも無ければタイトルのみと明示する。

Usage:
    python run_digest.py [YYYY-MM-DD]

Output:
    Full Obsidian Markdown to stdout (stderr for progress logs)
"""

import sys
import re
import os
import json
import time
import html
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",

)
JST = timezone(timedelta(hours=9))
TODAY = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")

RSS_FEEDS = [
    ("BleepingComputer",  "https://www.bleepingcomputer.com/feed/"),
    ("TheHackerNews",     "https://feeds.feedburner.com/TheHackersNews"),
    ("SecurityWeek",      "https://www.securityweek.com/feed/"),
    ("DarkReading",       "https://www.darkreading.com/rss.xml"),
    ("CiscoTalos",        "https://blog.talosintelligence.com/rss/"),
    ("JPCERT",            "https://www.jpcert.or.jp/rss/jpcert.rdf"),
    ("SCANNetSecurity",   "https://scan.netsecurity.ne.jp/rss20/index.rdf"),
]

MAX_ITEMS_PER_FEED = 8

# ── Article body fetching ─────────────────────────────────────────────────────
MAX_BODY_CHARS   = 1500   # 1記事あたりの本文上限 (トークン量の制御)
TOTAL_BODY_CHARS = 90000  # 全記事合計の上限。超えたら以降は要約のみ
FETCH_WORKERS    = 6      # 並列取得数 (実行時間を抑える)
FETCH_TIMEOUT    = 12     # 1記事あたりのタイムアウト秒

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Models to try in order (fallback chain)
# 2026-08-10: gemini-2.0-flash / 2.0-flash-lite は無料枠が実質使えず、
# 毎回 429 → 15秒待機 → 再試行 → 次モデル、で約40秒を空費していた
# （本文取得の追加前から発生。実測で 2.5-flash が初回成功）。
# 実際に通るモデルを先頭に並べ替えた。2.0 系は最後の保険として残す。
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


# ── RSS Collection ─────────────────────────────────────────────────────────────
def fetch_rss(url: str, timeout: int = 15) -> str:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CyberDigest/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: {url} → {e}", file=sys.stderr)
        return ""


def unescape_html(text: str) -> str:
    return html.unescape(text)


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape_html(text)).strip()


def parse_rss(xml: str, source: str) -> list[dict]:
    """RSS/RDF から title / url / description(要約) を取り出す。"""
    items = []
    blocks = re.findall(r"<item[\s\S]*?</item>", xml, re.IGNORECASE)[:MAX_ITEMS_PER_FEED]
    for block in blocks:
        t = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>",   block, re.IGNORECASE)
        l = (re.search(r"<link[^>]*>(?:<!\[CDATA\[)?(https?://[^\s<\]]+)(?:\]\]>)?</link>", block, re.IGNORECASE) or
             re.search(r"<guid[^>]*>(https?://[^\s<]+)</guid>",                               block, re.IGNORECASE))
        if not (t and l):
            continue
        title = re.sub(r"<[^>]+>", "", unescape_html(t.group(1))).strip()
        url   = l.group(1).strip()

        # RSS 側の要約 (本文取得に失敗したときのフォールバック)
        d = (re.search(r"<content:encoded[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</content:encoded>", block, re.IGNORECASE) or
             re.search(r"<description[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</description>",         block, re.IGNORECASE))
        rss_summary = strip_tags(d.group(1))[:MAX_BODY_CHARS] if d else ""

        if title and len(title) > 5 and not re.fullmatch(r"(RSS|Feed|Home|\s*)", title, re.IGNORECASE):
            items.append({
                "title": title,
                "url": url,
                "source": source,
                "rss_summary": rss_summary,
                "body": "",
                "body_origin": "none",
            })
    return items


def collect_news() -> list[dict]:
    all_items = []
    for source, url in RSS_FEEDS:
        print(f"  [{source}] 取得中...", file=sys.stderr)
        xml   = fetch_rss(url)
        items = parse_rss(xml, source)
        all_items.extend(items)
        print(f"  [{source}] {len(items)} 件", file=sys.stderr)
    return all_items


# ── Article body extraction (stdlib only) ─────────────────────────────────────
def html_to_text(raw_html: str) -> str:
    """HTML から本文らしいテキストを抽出する。外部ライブラリ不使用。"""
    # 1. ノイズタグを中身ごと除去
    noise = r"script|style|noscript|svg|form|nav|header|footer|aside|iframe|figure|button"
    cleaned = re.sub(rf"<({noise})[\s\S]*?</\1>", " ", raw_html, flags=re.IGNORECASE)
    cleaned = re.sub(r"<!--[\s\S]*?-->", " ", cleaned)

    # 2. 本文コンテナ候補をすべて集め、最も長いものを採用する
    #    （最初の <article> が「関連記事」等の小さい要素であるケースを避ける）
    #    属性はシングル/ダブル両方のクォートに対応（例: TheHackerNews は id='articlebody'）
    BODY_CLASS = r"(?:article-?body|entry-content|post-content|content-body|post-body|story-body)"
    candidates = []
    for pattern in (
        r"<article[^>]*>([\s\S]*?)</article>",
        r"<main[^>]*>([\s\S]*?)</main>",
        rf"<div[^>]*(?:class|id)=[\"'][^\"']*{BODY_CLASS}[^\"']*[\"'][^>]*>([\s\S]*?)</div>",
    ):
        for m in re.finditer(pattern, cleaned, re.IGNORECASE):
            frag = m.group(1)
            if len(frag) > 400:
                candidates.append(frag)

    scope = max(candidates, key=len) if candidates else cleaned

    # 3. <p> を優先して拾う。少なければ全体をテキスト化
    paras = re.findall(r"<p[^>]*>([\s\S]*?)</p>", scope, re.IGNORECASE)
    texts = [strip_tags(p) for p in paras]
    texts = [t for t in texts if len(t) > 40]

    if len(" ".join(texts)) < 200:
        body = strip_tags(scope)
    else:
        body = "\n".join(texts)

    return re.sub(r"\s+\n", "\n", body).strip()


def fetch_article_body(item: dict) -> dict:
    """1記事の本文を取得。失敗しても例外を投げず item を返す。"""
    url = item["url"]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                raise ValueError(f"non-html content-type: {ctype[:40]}")
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(600_000).decode(charset, errors="replace")

        body = html_to_text(raw)
        if len(body) >= 200:
            item["body"] = body[:MAX_BODY_CHARS]
            item["body_origin"] = "fulltext"
            return item
        raise ValueError(f"body too short ({len(body)} chars)")

    except Exception as e:
        # フォールバック: RSS の description
        if item.get("rss_summary"):
            item["body"] = item["rss_summary"]
            item["body_origin"] = "rss"
        else:
            item["body"] = ""
            item["body_origin"] = "none"
        print(f"  [body] 失敗 → {item['body_origin']}: {url} ({e})", file=sys.stderr)
        return item


def enrich_with_bodies(items: list[dict]) -> list[dict]:
    """全記事の本文を並列取得し、合計文字数の上限も守る。"""
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        items = list(ex.map(fetch_article_body, items))

    # 合計上限を超えたら、超過分は本文を落として要約扱いにする
    total = 0
    for it in items:
        if total + len(it["body"]) > TOTAL_BODY_CHARS:
            it["body"] = it["body"][:300]
            if it["body_origin"] == "fulltext":
                it["body_origin"] = "truncated"
        total += len(it["body"])

    stats = {}
    for it in items:
        stats[it["body_origin"]] = stats.get(it["body_origin"], 0) + 1
    print(f"  本文取得結果: {stats}（合計 {total:,} 文字）", file=sys.stderr)
    return items


# ── Gemini Analysis (plain HTTP, no SDK) ───────────────────────────────────────
def _call_gemini_http(model_name: str, prompt: str) -> str:
    url = f"{GEMINI_BASE}/{model_name}:generateContent?key={GEMINI_API_KEY}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _gemini_with_retry(prompt: str) -> str:
    last_err = None
    for model_name in GEMINI_MODELS:
        for attempt in range(2):
            try:
                print(f"  モデル: {model_name} (attempt {attempt+1})...", file=sys.stderr)
                return _call_gemini_http(model_name, prompt)
            except urllib.error.HTTPError as e:
                last_err = e
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code == 429 or "quota" in body_text.lower() or "exhausted" in body_text.lower():
                    if attempt == 0:
                        print(f"  レート制限。15秒後にリトライ...", file=sys.stderr)
                        time.sleep(15)
                    else:
                        print(f"  {model_name} クォータ超過。次のモデルを試みます...", file=sys.stderr)
                        break
                elif e.code == 403 and "leaked" in body_text.lower():
                    print(f"  {model_name} APIキーが無効。次のモデルを試みます...", file=sys.stderr)
                    break
                else:
                    print(f"  {model_name} エラー {e.code}: {body_text[:100]}", file=sys.stderr)
                    break
            except Exception as e:
                last_err = e
                print(f"  {model_name} 例外: {e}", file=sys.stderr)
                break
    raise RuntimeError(f"全モデルでGemini API呼び出しに失敗しました: {last_err}")


ORIGIN_LABEL = {
    "fulltext":  "本文",
    "rss":       "RSS要約のみ",
    "truncated": "本文(冒頭のみ)",
    "none":      "本文取得不可・タイトルのみ",
}


def build_articles_block(items: list[dict]) -> str:
    chunks = []
    for i, item in enumerate(items, 1):
        label = ORIGIN_LABEL.get(item["body_origin"], item["body_origin"])
        body  = item["body"].strip() or "(本文なし)"
        chunks.append(
            f"### 記事{i}\n"
            f"- ソース: {item['source']}\n"
            f"- タイトル: {item['title']}\n"
            f"- URL: {item['url']}\n"
            f"- 本文種別: {label}\n"
            f"- 本文:\n{body}\n"
        )
    return "\n".join(chunks)


_URL_RE = re.compile(r'https?://[^\s\)\]<>"]+')


def _url_key(u: str) -> str:
    """比較用の正規化キー。大文字小文字と末尾スラッシュの差を吸収する。"""
    return u.lower().rstrip("/")


def repair_urls(analysis: str, items: list[dict]) -> str:
    """AI が出力した URL を、実際に収集した URL に強制的に揃える。

    本文を渡しても LLM は URL の綴りを「整形」してしまう。
    実測 (2026-08-25, Cisco 28記事): 出力51URL中 12件 (24%) が改変されていた。
      例) .../achieves-fedramp-class-d-...  →  .../achieves-fedRAMP-class-d-...
    URL のパスは大文字小文字を区別するため、これはそのままリンク切れになる。
    プロンプトで禁じるだけでは防げないので、機械的に実 URL へ付け替える。
    """
    real = {_url_key(it["url"]): it["url"] for it in items}
    repaired, unknown = [], []

    def _sub(m):
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in ".,;:":      # 文末句読点は URL に含めない
            trail, raw = raw[-1] + trail, raw[:-1]
        fixed = real.get(_url_key(raw))
        if fixed is None:
            unknown.append(raw)
            return raw + trail
        if fixed != raw:
            repaired.append((raw, fixed))
        return fixed + trail

    result = _URL_RE.sub(_sub, analysis)

    if repaired:
        print(f"  [url] {len(repaired)} 件の URL を実物へ修復:", file=sys.stderr)
        for before, after in repaired[:5]:
            print(f"    - {before}\n      → {after}", file=sys.stderr)
    if unknown:
        print(f"  [url] WARNING: 収集記事に無い URL {len(unknown)} 件（AI の創作の疑い）:",
              file=sys.stderr)
        for u in unknown[:5]:
            print(f"    - {u}", file=sys.stderr)
    if not repaired and not unknown:
        print("  [url] 全URLが実物と一致", file=sys.stderr)
    return result


def analyze_with_gemini(items: list[dict]) -> str:
    articles_text = build_articles_block(items)
    prompt = f"""あなたはサイバーセキュリティの専門家アナリストです。
以下は今日（{TODAY}）収集したセキュリティニュース記事の本文です。
**必ず提供された本文の記述のみを根拠に**、6つのカテゴリ別に日本語で要約してください。

## 最重要ルール（厳守）
- **本文に書かれていない事実を創作しない**。推測を書く場合は「〜と見られる」等と明示する
- CVE番号・CVSSスコア・被害規模・製品名は、**本文に明記されている場合のみ**記載する
- 本文種別が「RSS要約のみ」「本文取得不可・タイトルのみ」の記事は、
  情報が限定的である前提で慎重に扱い、断定を避ける
- 本文に根拠がない項目は「不明」と書く（数値をでっち上げない）

【収集した記事】
{articles_text}

## 出力ルール
- 各カテゴリのセクション見出しは必ず出力する（該当なしでも）
- 該当ありの場合、見出しは**必ず Markdown リンク形式**で書く: `### [タイトル](URL)`
  - `### タイトル (URL)` のように URL を丸括弧で並べる書き方は禁止
  - 見出しの次の行に「- **概要**: 1〜2文」を書く
- **URLは提供されたものを1文字も変えずにコピーする**（大文字小文字も変えない）
- 脆弱性情報には「- **CVSS**: スコアまたは不明」「- **対応**: パッチ有無・推奨アクション」も追記
- 業界動向は箇条書き「- [タイトル](URL) — 一言まとめ」
- 該当なしの場合: 「- 該当なし」
- 余計な説明や前置きは不要。セクション見出しから即出力すること

## 🔴 重大インシデント
（ランサムウェア・大規模漏洩・国家関与の攻撃）

## 🟠 脆弱性情報
（CVE・ゼロデイ・パッチ情報）

## 🟡 攻撃キャンペーン
（フィッシング・マルウェア・APTキャンペーン）

## 🤖 AIセキュリティ
（AI悪用・プロンプトインジェクション・LLM関連）

## 🔵 業界動向
（規制・法令・業界トレンド）

## 🇯🇵 国内情報
（JPCERT・SCAN NetSecurityの情報）
"""
    return _gemini_with_retry(prompt)


def analyze_with_gemini_fallback() -> str:
    """Generate digest from Gemini's knowledge when RSS is unavailable."""
    prompt = f"""あなたはサイバーセキュリティの専門家アナリストです。
今日（{TODAY}）時点での最新のサイバーセキュリティ情報を、あなたの知識をもとに6つのカテゴリ別に日本語で要約してください。
実際に報告されている・報告が予想されるセキュリティトピックを取り上げてください。

## 出力ルール
- 各カテゴリのセクション見出しは必ず出力する（該当なしでも）
- 該当ありの場合: 「### タイトル\\n- **概要**: 1〜2文」
- 脆弱性情報には「- **CVSS**: スコアまたは不明」「- **対応**: パッチ有無・推奨アクション」も追記
- 業界動向は箇条書き「- タイトル — 一言まとめ」
- 該当なしの場合: 「- 該当なし」
- 余計な説明や前置きは不要。セクション見出しから即出力すること
- ※RSS収集不可のため、Gemini知識ベースによる生成であることを最後に注記

## 🔴 重大インシデント
（ランサムウェア・大規模漏洩・国家関与の攻撃）

## 🟠 脆弱性情報
（CVE・ゼロデイ・パッチ情報）

## 🟡 攻撃キャンペーン
（フィッシング・マルウェア・APTキャンペーン）

## 🤖 AIセキュリティ
（AI悪用・プロンプトインジェクション・LLM関連）

## 🔵 業界動向
（規制・法令・業界トレンド）

## 🇯🇵 国内情報
（JPCERT・SCAN NetSecurityの情報）
"""
    return _gemini_with_retry(prompt)


# ── Markdown Builder ───────────────────────────────────────────────────────────
def build_markdown(items: list[dict], analysis: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    if items:
        sources   = sorted({item["source"] for item in items})
        full      = sum(1 for i in items if i["body_origin"] in ("fulltext", "truncated"))
        rss_only  = sum(1 for i in items if i["body_origin"] == "rss")
        title_only = sum(1 for i in items if i["body_origin"] == "none")
        meta = (
            f"収集件数: {len(items)}件"
            f"（本文取得 {full} / RSS要約 {rss_only} / タイトルのみ {title_only}）"
            f" ／ ソース: {', '.join(sources)} ＋ Gemini分析"
        )
        src_field = "rss+fulltext+gemini"
    else:
        meta = "RSS収集不可 ／ Gemini知識ベースによる生成"
        src_field = "gemini-knowledge"
    return f"""---
tags:
  - cyber-digest
  - security
date: {TODAY}
source: {src_field}
---

# 🛡️ サイバーセキュリティ・ダイジェスト {TODAY}

> {meta}

{analysis.strip()}

---
*収集日時: {now} JST*
"""


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"🛡️  Cyber Digest {TODAY}", file=sys.stderr)

    print("\n📡 Step 1: RSSフィードを収集中...", file=sys.stderr)
    items = collect_news()
    print(f"\n  合計 {len(items)} 件収集完了", file=sys.stderr)

    rss_failed = not items
    if rss_failed:
        print("  WARNING: RSS取得失敗。Gemini知識ベースでダイジェストを生成します。", file=sys.stderr)
    else:
        print(f"\n📰 Step 2: 各記事の本文を取得中（並列{FETCH_WORKERS})...", file=sys.stderr)
        items = enrich_with_bodies(items)

    print("\n🤖 Step 3: Gemini APIで分析中...", file=sys.stderr)
    if rss_failed:
        analysis = analyze_with_gemini_fallback()
    else:
        analysis = analyze_with_gemini(items)
    print("  分析完了", file=sys.stderr)

    if not rss_failed:
        # AI が書き換えた URL を実物へ戻す (フォールバック時は照合先が無いので行わない)
        analysis = repair_urls(analysis, items)

    print("\n📝 Step 4: Markdown生成中...", file=sys.stderr)
    markdown = build_markdown(items, analysis)

    # Output to stdout
    sys.stdout.buffer.write(markdown.encode("utf-8"))
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
