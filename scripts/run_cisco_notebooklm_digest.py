#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cisco Digest via NotebookLM
RSS記事URLをNotebookLMに追加→分析クエリ→Obsidianダイジェスト生成

Usage:
    python run_cisco_notebooklm_digest.py [YYYY-MM-DD]

Environment variables:
    NOTEBOOKLM_SESSION  : base64エンコードされたstorage_state.json
    CISCO_NOTEBOOK_ID   : NotebookLMノートブックID（省略時は自動作成）

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
JST           = timezone(timedelta(hours=9))
TODAY         = sys.argv[1] if len(sys.argv) > 1 else datetime.now(JST).strftime("%Y-%m-%d")

NOTEBOOK_TITLE = "CiscoDigest"
NOTEBOOK_ID_FILE = Path.home() / ".notebooklm" / "cisco_notebook_id.txt"
MAX_URLS      = 12
URLS_PER_FEED = 4

RSS_FEEDS = [
    ("CiscoSecurityBlog", "https://blogs.cisco.com/security/feed"),
    ("CiscoJapanBlog",    "https://gblogs.cisco.com/jp/feed/"),
    ("CiscoBlog",         "https://blogs.cisco.com/feed"),
]

QUERY_PROMPT = """
今日（{date}）取得したCiscoに関する最新情報について、以下の形式で日本語で分析してください。

## 🛡️ セキュリティプロダクト
Cisco製品のセキュリティ機能・新機能・アップデート情報。各項目：タイトル（日本語）/ 概要2〜3文

## 🔒 セキュリティアドバイザリ
CVE・脆弱性・パッチ情報。各項目：タイトル / CVE番号（あれば） / 概要 / 推奨対応

## 🔗 ネットワーク・インフラ
ネットワーク製品・データセンター・クラウドインフラ情報。箇条書きで3〜5件。

## 🤖 AI・自動化
Cisco AIソリューション・自動化・DevNet情報。各項目：タイトル / 概要1〜2文

## 🇯🇵 国内情報
日本語ブログ（gblogs.cisco.com/jp）に掲載された情報。

各カテゴリに該当がない場合は「- 該当なし」と記載してください。
ソースに記載のない情報は追加しないでください。
"""

# ── Session restore ───────────────────────────────────────────────────────────
def restore_session():
    """GitHub ActionsのSecretからセッションを復元"""
    session_b64 = os.environ.get("NOTEBOOKLM_SESSION", "")
    if not session_b64:
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
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CiscoDigest/2.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: {url} -> {e}", file=sys.stderr)
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

    seen: set[str] = set()
    unique: list[str] = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[:MAX_URLS]


# ── NotebookLM Operations ─────────────────────────────────────────────────────
async def get_or_create_notebook(client) -> str:
    """ノートブックIDを取得または新規作成する"""
    # 環境変数で明示指定されている場合はそちらを優先
    env_id = os.environ.get("CISCO_NOTEBOOK_ID", "")
    if env_id:
        print(f"  環境変数からノートブックID使用: {env_id}", file=sys.stderr)
        return env_id

    # ローカルファイルにキャッシュされている場合
    if NOTEBOOK_ID_FILE.exists():
        cached_id = NOTEBOOK_ID_FILE.read_text().strip()
        if cached_id:
            print(f"  キャッシュからノートブックID使用: {cached_id}", file=sys.stderr)
            return cached_id

    # 既存ノートブック一覧から検索
    print(f"  ノートブック一覧を検索中...", file=sys.stderr)
    notebooks = await client.notebooks.list()
    for nb in notebooks:
        if nb.title == NOTEBOOK_TITLE:
            print(f"  既存ノートブック発見: {nb.id}", file=sys.stderr)
            NOTEBOOK_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            NOTEBOOK_ID_FILE.write_text(nb.id)
            return nb.id

    # 新規作成
    print(f"  '{NOTEBOOK_TITLE}' ノートブックを新規作成中...", file=sys.stderr)
    nb = await client.notebooks.create(title=NOTEBOOK_TITLE)
    print(f"  作成完了: {nb.id}", file=sys.stderr)
    NOTEBOOK_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTEBOOK_ID_FILE.write_text(nb.id)
    return nb.id


async def run_notebooklm(article_urls: list[str]) -> str:
    from notebooklm import NotebookLMClient

    async with await NotebookLMClient.from_storage() as client:

        # ノートブックID取得または作成
        notebook_id = await get_or_create_notebook(client)

        # 既存URLソースを削除
        print("  既存URLソースを確認・削除中...", file=sys.stderr)
        try:
            sources = await client.sources.list(notebook_id)
            url_sources = [s for s in sources if s.kind == "url"]
            for s in url_sources:
                await client.sources.delete(notebook_id, s.id)
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
                    client.sources.add_url(notebook_id, url, wait=True),
                    timeout=120
                )
                added += 1
                print(f"  OK", file=sys.stderr)
            except asyncio.TimeoutError:
                print(f"  タイムアウト（スキップ）", file=sys.stderr)
            except Exception as e:
                print(f"  エラー: {e}", file=sys.stderr)

        print(f"\n  {added}/{len(article_urls)} ソース追加完了", file=sys.stderr)

        if added == 0:
            raise RuntimeError("ソースを1件も追加できませんでした")

        # クエリ送信
        print("\n  NotebookLMにクエリ送信中...", file=sys.stderr)
        query = QUERY_PROMPT.format(date=TODAY)
        result = await client.chat.ask(notebook_id, query)
        print("  クエリ完了", file=sys.stderr)
        return result.answer


# ── Output Formatting ─────────────────────────────────────────────────────────
def format_output(analysis: str, url_count: int) -> str:
    return f"""---
tags:
  - cisco-digest
  - cisco
  - security
date: {TODAY}
source: notebooklm+rss
---

# \U0001f535 Cisco ダイジェスト {TODAY}

> ソース: RSS {len(RSS_FEEDS)}フィード ({url_count}記事) ／ NotebookLM分析

{analysis.strip()}

---
*収集日時: {TODAY} 08:00 JST*
"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\U0001f535  Cisco Digest (NotebookLM) {TODAY}", file=sys.stderr)

    print("\n\U0001f511 Step 1: セッション復元中...", file=sys.stderr)
    restore_session()

    print("\n\U0001f4e1 Step 2: RSS記事URLを収集中...", file=sys.stderr)
    article_urls = collect_article_urls()
    print(f"\n  合計 {len(article_urls)} 件のURLを収集", file=sys.stderr)

    if not article_urls:
        print("  WARNING: URLを収集できませんでした", file=sys.stderr)
        sys.exit(1)

    print("\n\U0001f916 Step 3: NotebookLMで分析中（数分かかります）...", file=sys.stderr)
    try:
        analysis = asyncio.run(run_notebooklm(article_urls))
    except Exception as e:
        print(f"\n  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n\U0001f4dd Step 4: Markdown生成中...", file=sys.stderr)
    output = format_output(analysis, len(article_urls))
    print(output)
    print("\n✅ 完了", file=sys.stderr)


if __name__ == "__main__":
    main()
