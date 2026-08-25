#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cisco Digest - RSS Collection + Article Body Extraction + Gemini Analysis

2026-08-25 改訂:
  Cyber Digest 側 (run_digest.py) で 2026-08-10 に導入した「記事本文を取得してから
  Gemini に渡す」設計を移植した。旧版はタイトルと URL だけを渡していたため、
  AI が中身を推測して書き、URL のスラッグまで捏造する事故が起きていた。

  本文取得・Gemini 呼び出し・記事ブロック整形は **run_digest.py から import** する。
  コピーせず共有することで、今後 Cyber 側の改善が Cisco 側にも自動的に効く。

Usage:
    python run_cisco_digest.py [YYYY-MM-DD]

Output:
    Full Obsidian Markdown to stdout (stderr for progress logs)
"""

import sys
import re
from datetime import datetime, timezone, timedelta

# 同じ scripts/ ディレクトリの run_digest.py から共通処理を借りる。
# run_digest.py は main() を __main__ ガードしているので import しても副作用はない。
from run_digest import (
    unescape_html,
    strip_tags,
    enrich_with_bodies,
    build_articles_block,
    repair_urls,
    _gemini_with_retry,
    MAX_BODY_CHARS,
)

# ── Configuration ─────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
TODAY = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")

RSS_FEEDS = [
    ("CiscoSecurityBlog", "https://blogs.cisco.com/security/feed"),
    ("CiscoJapanBlog",    "https://gblogs.cisco.com/jp/feed/"),
    ("CiscoBlog",         "https://blogs.cisco.com/feed"),
]

MAX_ITEMS_PER_FEED = 10


# ── RSS Collection ─────────────────────────────────────────────────────────────
def fetch_rss(url: str, timeout: int = 15) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CiscoDigest/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: {url} → {e}", file=sys.stderr)
        return ""


def parse_rss(xml: str, source: str) -> list[dict]:
    """RSS/RDF から title / url / description(要約) を取り出す。

    rss_summary は本文取得に失敗したときのフォールバックとして使うため必須。
    """
    items = []
    for block in re.findall(r"<item[\s\S]*?</item>", xml, re.IGNORECASE)[:MAX_ITEMS_PER_FEED]:
        t = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>",   block, re.IGNORECASE)
        l = (re.search(r"<link[^>]*>(?:<!\[CDATA\[)?(https?://[^\s<\]]+)(?:\]\]>)?</link>", block, re.IGNORECASE) or
             re.search(r"<guid[^>]*>(https?://[^\s<]+)</guid>",                               block, re.IGNORECASE))
        if not (t and l):
            continue
        title = re.sub(r"<[^>]+>", "", unescape_html(t.group(1))).strip()
        url   = l.group(1).strip()

        d = (re.search(r"<content:encoded[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</content:encoded>", block, re.IGNORECASE) or
             re.search(r"<description[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</description>",         block, re.IGNORECASE))
        rss_summary = strip_tags(d.group(1))[:MAX_BODY_CHARS] if d else ""

        if title and len(title) > 5 and not re.fullmatch(r"(RSS|Feed|Home|Cisco Blog|\s*)", title, re.IGNORECASE):
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
        items = parse_rss(fetch_rss(url), source)
        all_items.extend(items)
        print(f"  [{source}] {len(items)} 件", file=sys.stderr)

    # 3フィードは記事が重複するため URL で重複排除する
    seen, unique = set(), []
    for it in all_items:
        if it["url"] not in seen:
            seen.add(it["url"])
            unique.append(it)
    if len(unique) != len(all_items):
        print(f"  重複除去: {len(all_items)} → {len(unique)} 件", file=sys.stderr)
    return unique


# ── Gemini Analysis ────────────────────────────────────────────────────────────
def analyze_with_gemini(items: list[dict]) -> str:
    articles_text = build_articles_block(items)
    prompt = f"""あなたはCiscoセキュリティ製品の専門家アナリストです。
以下は今週（{TODAY}時点）収集したCisco関連ニュース・ブログ記事の本文です。
**必ず提供された本文の記述のみを根拠に**、プロダクトカテゴリ別に日本語で要約してください。

## 最重要ルール（厳守）
- **本文に書かれていない事実を創作しない**。推測を書く場合は「〜と見られる」等と明示する
- CVE番号・CVSSスコア・対象バージョン・製品名は、**本文に明記されている場合のみ**記載する
- **URLは提供されたものをそのまま使う**。URLを組み立てたり推測したりしない
- 本文種別が「RSS要約のみ」「本文取得不可・タイトルのみ」の記事は、
  情報が限定的である前提で慎重に扱い、断定を避ける
- 本文に根拠がない項目は「不明」と書く（数値をでっち上げない）

【収集した記事】
{articles_text}

## 出力ルール
- 各セクション見出しは必ず出力する（該当なしでも）
- 該当ありの場合、見出しは**必ず Markdown リンク形式**で書く: `### [タイトル](URL)`
  - `### タイトル (URL)` のように URL を丸括弧で並べる書き方は禁止
  - 見出しの次の行に「- **概要**: 1〜2文」を書く
- **URLは提供されたものを1文字も変えずにコピーする**（大文字小文字も変えない）
- 新機能・アップデートには「- **バージョン**: 対象バージョンまたは不明」も追記
- セキュリティアドバイザリには「- **CVE**: 番号または不明」「- **対応**: 推奨アクション」も追記
- 該当なしの場合: 「- 該当なし」
- 余計な説明や前置きは不要。セクション見出しから即出力すること

## 🛡️ セキュリティプロダクト
（Secure Firewall・Secure Endpoint・Umbrella・ISE・Duo・Secure Email・XDR・Talos関連）

## 🔒 セキュリティアドバイザリ
（CVE・脆弱性情報・パッチ・PSIRT情報）

## 🔗 ネットワーク・インフラ
（Catalyst・Meraki・SD-WAN・ACI・Catalyst Center関連のセキュリティ機能）

## 🤖 AI・自動化
（AIセキュリティ・自動化・Cisco XDR・AI活用事例）

## 🇯🇵 国内情報
（Cisco Japan Blogの記事・日本向け情報）

## 📰 その他
（上記に分類されないCisco関連情報）
"""
    return _gemini_with_retry(prompt)


# ── Markdown Builder ───────────────────────────────────────────────────────────
def build_markdown(items: list[dict], analysis: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    sources = sorted({item["source"] for item in items})

    n_full = sum(1 for i in items if i["body_origin"] in ("fulltext", "truncated"))
    n_rss  = sum(1 for i in items if i["body_origin"] == "rss")
    n_none = sum(1 for i in items if i["body_origin"] == "none")

    return f"""---
tags:
  - cisco-digest
  - cisco
  - security
date: {TODAY}
source: rss+fulltext+gemini
---

# 🔵 Cisco ダイジェスト {TODAY}

> 収集件数: {len(items)}件（本文取得 {n_full} / RSS要約 {n_rss} / タイトルのみ {n_none}） ／ ソース: {", ".join(sources)} ＋ Gemini分析

{analysis.strip()}

---
*収集日時: {now} JST*
"""


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"🔵 Cisco Digest {TODAY}", file=sys.stderr)

    print("\n📡 Step 1: RSSフィードを収集中...", file=sys.stderr)
    items = collect_news()
    print(f"\n  合計 {len(items)} 件収集完了", file=sys.stderr)

    if not items:
        print("ERROR: RSSから1件も取得できませんでした。", file=sys.stderr)
        sys.exit(1)

    print("\n📄 Step 2: 記事本文を取得中...", file=sys.stderr)
    items = enrich_with_bodies(items)

    print("\n🤖 Step 3: Gemini APIで分析中...", file=sys.stderr)
    analysis = analyze_with_gemini(items)
    print("  分析完了", file=sys.stderr)

    # AI が書き換えた URL を実物へ戻す
    analysis = repair_urls(analysis, items)

    print("\n📝 Step 4: Markdown生成中...", file=sys.stderr)
    markdown = build_markdown(items, analysis)

    sys.stdout.buffer.write(markdown.encode("utf-8"))
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
