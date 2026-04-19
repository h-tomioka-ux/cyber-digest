#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cisco Digest - RSS Collection + Gemini Analysis
Collects Cisco product news from RSS feeds and analyzes with Gemini 2.5 Flash API.

Usage:
    python run_cisco_digest.py [YYYY-MM-DD]

Output:
    Full Obsidian Markdown to stdout (stderr for progress logs)
"""

import sys
import re
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "AIzaSyCltNnGXBe9eeHW9U8XGiA3vJ86GryOKkY"
)
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash-lite",
]
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
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CiscoDigest/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: {url} → {e}", file=sys.stderr)
        return ""


def unescape_html(text: str) -> str:
    return (text
            .replace("&amp;",  "&")
            .replace("&lt;",   "<")
            .replace("&gt;",   ">")
            .replace("&#39;",  "'")
            .replace("&quot;", '"')
            .replace("&nbsp;", " "))


def parse_rss(xml: str, source: str) -> list[dict]:
    items = []
    for block in re.findall(r"<item[\s\S]*?</item>", xml, re.IGNORECASE)[:MAX_ITEMS_PER_FEED]:
        t = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>",   block, re.IGNORECASE)
        l = (re.search(r"<link[^>]*>(?:<!\[CDATA\[)?(https?://[^\s<\]]+)(?:\]\]>)?</link>", block, re.IGNORECASE) or
             re.search(r"<guid[^>]*>(https?://[^\s<]+)</guid>",                               block, re.IGNORECASE))
        if not (t and l):
            continue
        title = re.sub(r"<[^>]+>", "", unescape_html(t.group(1))).strip()
        url   = l.group(1).strip()
        if title and len(title) > 5 and not re.fullmatch(r"(RSS|Feed|Home|Cisco Blog|\s*)", title, re.IGNORECASE):
            items.append({"title": title, "url": url, "source": source})
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


# ── Gemini Analysis ────────────────────────────────────────────────────────────
def analyze_with_gemini(items: list[dict]) -> str:
    try:
        from google import genai
    except ImportError:
        print("ERROR: google-genai が未インストールです。", file=sys.stderr)
        print('  python.exe -m pip install google-genai', file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)

    titles_text = "\n".join(
        f"- [{item['source']}] {item['title']} - {item['url']}"
        for item in items
    )

    prompt = f"""あなたはCiscoセキュリティ製品の専門家アナリストです。
以下の今週（{TODAY}時点）のCisco関連ニュース・ブログ記事を分析し、
プロダクトカテゴリ別に日本語で要約してください。

【収集した記事タイトル一覧】
{titles_text}

## 出力ルール
- 各セクション見出しは必ず出力する（該当なしでも）
- 該当ありの場合: 「### [タイトル](URL)\\n- **概要**: 1〜2文」
- 新機能・アップデートには「- **バージョン**: 対象バージョン（あれば）」も追記
- セキュリティアドバイザリには「- **CVE**: 番号（あれば）」「- **対応**: 推奨アクション」も追記
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

    last_err = None
    for model_name in GEMINI_MODELS:
        for attempt in range(2):
            try:
                print(f"  モデル: {model_name} (attempt {attempt+1})...", file=sys.stderr)
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return response.text
            except Exception as e:
                last_err = e
                err = str(e)
                if "429" in err or "quota" in err.lower() or "exhausted" in err.lower():
                    if attempt == 0:
                        print(f"  レート制限。15秒後にリトライ...", file=sys.stderr)
                        time.sleep(15)
                    else:
                        print(f"  {model_name} クォータ超過。次のモデルを試みます...", file=sys.stderr)
                        break
                else:
                    raise

    raise RuntimeError(f"全モデルでGemini API呼び出しに失敗しました: {last_err}")


# ── Markdown Builder ───────────────────────────────────────────────────────────
def build_markdown(items: list[dict], analysis: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    sources = sorted({item["source"] for item in items})
    return f"""---
tags:
  - cisco-digest
  - cisco
  - security
date: {TODAY}
source: rss+gemini
---

# 🔵 Cisco ダイジェスト {TODAY}

> 収集件数: {len(items)}件 ／ ソース: {", ".join(sources)} ＋ Gemini 2.5 Flash分析

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

    print("\n🤖 Step 2: Gemini APIで分析中...", file=sys.stderr)
    analysis = analyze_with_gemini(items)
    print("  分析完了", file=sys.stderr)

    print("\n📝 Step 3: Markdown生成中...", file=sys.stderr)
    markdown = build_markdown(items, analysis)

    sys.stdout.buffer.write(markdown.encode("utf-8"))
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
