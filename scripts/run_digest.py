#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cyber Digest - RSS Collection + Gemini Analysis
Collects security news from RSS feeds and analyzes with Gemini API (plain HTTP).

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
import urllib.request
import urllib.error
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

# Models to try in order (fallback chain)
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
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
        if title and len(title) > 5 and not re.fullmatch(r"(RSS|Feed|Home|\s*)", title, re.IGNORECASE):
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


# ── Gemini Analysis (plain HTTP, no SDK) ───────────────────────────────────────
def _call_gemini_http(model_name: str, prompt: str) -> str:
    url = f"{GEMINI_BASE}/{model_name}:generateContent?key={GEMINI_API_KEY}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
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


def analyze_with_gemini(items: list[dict]) -> str:
    titles_text = "\n".join(
        f"- [{item['source']}] {item['title']} - {item['url']}"
        for item in items
    )
    prompt = f"""あなたはサイバーセキュリティの専門家アナリストです。
以下の今日（{TODAY}）のセキュリティニュースタイトル一覧を分析し、6つのカテゴリ別に日本語で要約してください。

【収集したニュースタイトル一覧】
{titles_text}

## 出力ルール
- 各カテゴリのセクション見出しは必ず出力する（該当なしでも）
- 該当ありの場合: 「### [タイトル](URL)\\n- **概要**: 1〜2文」
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
        sources = sorted({item["source"] for item in items})
        meta = f"収集件数: {len(items)}件 ／ ソース: {', '.join(sources)} ＋ Gemini分析"
    else:
        meta = "RSS収集不可 ／ Gemini知識ベースによる生成"
    return f"""---
tags:
  - cyber-digest
  - security
date: {TODAY}
source: rss+gemini
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

    print("\n🤖 Step 2: Gemini APIで分析中...", file=sys.stderr)
    if rss_failed:
        analysis = analyze_with_gemini_fallback()
    else:
        analysis = analyze_with_gemini(items)
    print("  分析完了", file=sys.stderr)

    print("\n📝 Step 3: Markdown生成中...", file=sys.stderr)
    markdown = build_markdown(items, analysis)

    # Output to stdout — Claude will capture and save to vault
    sys.stdout.buffer.write(markdown.encode("utf-8"))
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
