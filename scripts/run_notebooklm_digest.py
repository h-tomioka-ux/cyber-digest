#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cyber Digest via NotebookLM
RSS記事URLをNotebookLMに追加→分析クエリ→Obsidianダイジェスト生成

Usage:
    python run_notebooklm_digest.py [YYYY-MM-DD]

Environment variables:
    NOTEBOOKLM_SESSION : base64エンコードされたstorage_state.json

Output:
    Obsidian Markdown to stdout (progress to stderr)
"""

import asyncio
import base64
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
JST      = timezone(timedelta(hours=9))
TODAY    = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")

NOTEBOOK_ID   = "e572663d-4b25-4d8b-9917-d5f391f56eab"   # CyberDigest notebook
MAX_URLS      = 12   # 1回に追加するURL上限（処理時間のバランス）
URLS_PER_FEED = 3    # フィードあたりの最大URL数

RSS_FEEDS = [
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("TheHackerNews",    "https://feeds.feedburner.com/TheHackersNews"),
    ("SecurityWeek",     "https://www.securityweek.com/feed/"),
    ("DarkReading",      "https://www.darkreading.com/rss.xml"),
    ("JPCERT",           "https://www.jpcert.or.jp/rss/jpcert.rdf"),
    ("SCANNetSecurity",  "https://scan.netsecurity.ne.jp/rss20/index.rdf"),
]

QUERY_PROMPT = """
今日（{date}）取得したサイバーセキュリティニュースについて、以下の形式で日本語で分析してください。

## 🔴 重大インシデント
ランサムウェア・大規模漏洩・国家関与のインシデント。各項目：タイトル（日本語）/ 概要2〜3文

## 🟠 脆弱性情報
CVE・ゼロデイ・パッチ情報。各項目：タイトル / CVSSスコア / 概要 / 推奨対応

## 🟡 攻撃キャンペーン
フィッシング・マルウェア・APT活動。各項目：タイトル / 概要2〜3文

## 🤖 AIセキュリティ
AI悪用・プロンプトインジェクション・LLM関連の脅威。

## 🔵 業界動向
規制・法令・標準化・業界トレンド。箇条書きで3〜5件。

## 🇯🇵 国内情報
JPCERTやSCANに関連する国内の情報。

各カテゴリに該当がない場合は「- 該当なし」と記載してください。
ソースに記載のない情報は追加しないでください。
"""

# ── Session restore ───────────────────────────────────────────────────────────
def restore_session():
    """GitHub ActionsのSecretからセッションを復元"""
    session_b64 = os.environ.get("NOTEBOOKLM_SESSION", "")
    if not session_b64:
        # ローカル実行時はデフォルトパスを使用
        default = Path.home() / ".notebooklm" / "storage_state.json"
        if default.exists():
            print(f"  ローカルセッション使用: {default}", file=sys.stderr)
            return
        print("  ERROR: NOTEBOOKLM_SESSION 未設定かつローカルセッションなし", file=sys.stderr)
        sys.exit(1)

    home = Path.home() / ".notebooklm"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage = home / "storage_state.json"
    storage.write_bytes(base64.b64decode(session_b64))
    print(f"  セッション復元完了: {storage}", file=sys.stderr)


# ── RSS Collection ────────────────────────────────────────────────────────────
def fetch_rss(url: str, timeout: int = 15) -> str:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CyberDigest/2.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: {url} → {e}", file=sys.stderr)
        return ""


def parse_rss_urls(xml: str, max_items: int = URLS_PER_FEED) -> list[str]:
    urls = []
    for block in re.findall(r"<item[\s\S]*?</item>", xml, re.IGNORECASE)[:max_items]:
        m = (re.search(r"<link[^>]*>\s*(https?://[^\s<\]]+)\s*</link>", block, re.IGNORECASE) or
             re.search(r"<link[^>]*href=[\"'](https?://[^\"']+)[\"']", block, re.IGNORECASE))
        if m:
            urls.append(m.group(1).strip())
    return urls


def collect_article_urls() -> list[str]:
    all_urls: list[str] = []
    for name, feed_url in RSS_FEEDS:
        print(f"  [{name}] 取得中...", file=sys.stderr)
        xml = fetch_rss(feed_url)
        urls = parse_rss_urls(xml)
        print(f"  [{name}] {len(urls)} 件", file=sys.stderr)
        all_urls.extend(urls)

    # 重複除去して上限適用
    seen: set[str] = set()
    unique: list[str] = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[:MAX_URLS]


# ── NotebookLM Operations ─────────────────────────────────────────────────────
async def run_notebooklm(article_urls: list[str]) -> str:
    from notebooklm import NotebookLMClient

    async with await NotebookLMClient.from_storage() as client:

        # 既存URLソースを削除（前回分のクリア）
        print("  既存URLソースを確認・削除中...", file=sys.stderr)
        try:
            sources = await client.sources.list(NOTEBOOK_ID)
            url_sources = [s for s in sources if getattr(s, 'url', None)]
            for s in url_sources:
                await client.sources.delete(NOTEBOOK_ID, s.id)
                print(f"  削除: {s.title[:50]}", file=sys.stderr)
            print(f"  {len(url_sources)} 件のURLソースを削除", file=sys.stderr)
        except Exception as e:
            print(f"  ソース削除エラー（続行）: {e}", file=sys.stderr)

        # 新しいURLを追加
        print(f"\n  {len(article_urls)} 件のURLをNotebookLMに追加中...", file=sys.stderr)
        added = 0
        for i, url in enumerate(article_urls, 1):
            try:
                print(f"  [{i}/{len(article_urls)}] {url[:80]}", file=sys.stderr)
                await asyncio.wait_for(
                    client.sources.add_url(NOTEBOOK_ID, url, wait=True),
                    timeout=120   # 1URL最大2分
                )
                added += 1
                print(f"  ✓ 完了", file=sys.stderr)
            except asyncio.TimeoutError:
                print(f"  ✗ タイムアウト（スキップ）", file=sys.stderr)
            except Exception as e:
                print(f"  ✗ エラー: {e}", file=sys.stderr)

        print(f"\n  {added}/{len(article_urls)} ソース追加完了", file=sys.stderr)

        if added == 0:
            raise RuntimeError("ソースを1件も追加できませんでした")

        # NotebookLMにクエリ
        print("\n  NotebookLMにクエリ送信中...", file=sys.stderr)
        query = QUERY_PROMPT.format(date=TODAY)
        result = await client.chat.ask(NOTEBOOK_ID, query)
        print("  クエリ完了", file=sys.stderr)
        return result.answer


# ── Output Formatting ─────────────────────────────────────────────────────────
def format_output(analysis: str, url_count: int) -> str:
    return f"""---
tags:
  - cyber-digest
  - security
date: {TODAY}
source: notebooklm+rss
---

# 🛡️ サイバーセキュリティ・ダイジェスト {TODAY}

> ソース: RSS {len(RSS_FEEDS)}フィード ({url_count}記事) ／ NotebookLM分析

{analysis.strip()}

---
*収集日時: {TODAY} 08:00 JST*
"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🛡️  Cyber Digest (NotebookLM) {TODAY}", file=sys.stderr)

    # Step 1: セッション復元
    print("\n🔑 Step 1: セッション復元中...", file=sys.stderr)
    restore_session()

    # Step 2: RSS収集
    print("\n📡 Step 2: RSS記事URLを収集中...", file=sys.stderr)
    article_urls = collect_article_urls()
    print(f"\n  合計 {len(article_urls)} 件のURLを収集", file=sys.stderr)

    if not article_urls:
        print("  WARNING: URLを収集できませんでした", file=sys.stderr)
        sys.exit(1)

    # Step 3: NotebookLM分析
    print("\n🤖 Step 3: NotebookLMで分析中（数分かかります）...", file=sys.stderr)
    try:
        analysis = asyncio.run(run_notebooklm(article_urls))
    except Exception as e:
        print(f"\n  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 4: 出力
    print("\n📝 Step 4: Markdown生成中...", file=sys.stderr)
    output = format_output(analysis, len(article_urls))
    print(output)
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
