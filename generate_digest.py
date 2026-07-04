#!/usr/bin/env python3
"""Daily digest generator: fetches configured sources, calls Claude, writes HTML."""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import httpx
import yaml

REPO_ROOT = Path(__file__).parent
SOURCES_FILE = REPO_ROOT / "sources.yaml"
ARCHIVE_DIR = REPO_ROOT / "archive"
INDEX_FILE = REPO_ROOT / "index.html"

CEST = timezone(timedelta(hours=2))

# Shared CSS for all digest pages
CSS = """\
  :root {
    --bg: #fafaf7;
    --fg: #1a1a1a;
    --muted: #666;
    --accent: #2a5a8a;
    --border: #e5e5e0;
    --card: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #161616;
      --fg: #e8e8e8;
      --muted: #999;
      --accent: #7ab0e0;
      --border: #2a2a2a;
      --card: #1f1f1f;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: ui-serif, Georgia, "Times New Roman", serif;
    background: var(--bg);
    color: var(--fg);
    max-width: 760px;
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
    line-height: 1.55;
    font-size: 17px;
  }
  header {
    border-bottom: 1px solid var(--border);
    padding-bottom: 1.25rem;
    margin-bottom: 2rem;
  }
  h1 {
    font-size: 1.85rem;
    margin: 0 0 .25rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .date {
    color: var(--muted);
    font-size: 0.95rem;
    font-family: ui-sans-serif, system-ui, sans-serif;
  }
  h2 {
    font-size: 1.15rem;
    margin: 2.25rem 0 .75rem;
    font-family: ui-sans-serif, system-ui, sans-serif;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent);
    font-weight: 600;
  }
  section { margin-bottom: 1.5rem; }
  .item {
    padding: .85rem 0;
    border-bottom: 1px solid var(--border);
  }
  .item:last-child { border-bottom: none; }
  .item-title {
    font-weight: 600;
    margin: 0 0 .25rem;
  }
  .item-title a {
    color: var(--fg);
    text-decoration: none;
  }
  .item-title a:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  .item-meta {
    font-size: 0.85rem;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    margin-bottom: .35rem;
  }
  .item-summary {
    margin: 0;
    color: var(--fg);
  }
  .window-note {
    font-size: 0.88rem;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    margin: 0 0 .75rem;
  }
  .archive-section {
    margin-top: 3.5rem;
    padding-top: 1.5rem;
    border-top: 2px solid var(--border);
  }
  .archive-section h2 {
    margin-top: 0;
  }
  .archive-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .archive-list li {
    padding: .5rem 0;
    border-bottom: 1px solid var(--border);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.95rem;
  }
  .archive-list li:last-child { border-bottom: none; }
  .archive-list a {
    color: var(--accent);
    text-decoration: none;
    font-weight: 600;
  }
  .archive-list a:hover { text-decoration: underline; }
  .archive-summary {
    color: var(--muted);
    font-size: 0.88rem;
    margin-left: .5rem;
  }
  footer {
    margin-top: 3rem;
    padding-top: 1.25rem;
    border-top: 1px solid var(--border);
    font-size: 0.85rem;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
  }
  footer a { color: var(--muted); }"""


def strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def fetch_source(url: str, client: httpx.Client) -> str:
    try:
        r = client.get(url, follow_redirects=True, timeout=20)
        r.raise_for_status()
        return strip_html(r.text)[:6000]
    except Exception as exc:
        return f"[fetch failed: {exc}]"


def read_existing_archive_list() -> str:
    """Extract the <li> entries from the existing index.html archive list."""
    if not INDEX_FILE.exists():
        return ""
    content = INDEX_FILE.read_text()
    m = re.search(r'<ul class="archive-list">(.*?)</ul>', content, re.DOTALL)
    return m.group(1).strip() if m else ""


def build_prompt(config: dict, fetched: dict[str, str], now: datetime, existing_archive_li: str) -> str:
    settings = config.get("settings", {})
    max_per_source = settings.get("max_items_per_source", 3)
    max_per_topic = settings.get("max_items_per_topic", 6)
    lookback_h = settings.get("lookback_hours", 36)
    dedupe = settings.get("dedupe_near_duplicates", True)

    date_long = now.strftime("%-d %B %Y")
    time_cest = now.strftime("%H:%M CEST")
    date_iso = now.strftime("%Y-%m-%d")

    sources_block = []
    for topic in config.get("topics", []):
        topic_name = topic["name"]
        topic_filter = topic.get("filter", "")
        topic_type = topic.get("type", "")
        sources_block.append(f"\n### Topic: {topic_name}")
        if topic_filter:
            sources_block.append(f"Filter: {topic_filter}")
        if topic_type:
            sources_block.append(f"Type: {topic_type}")
        for src in topic.get("sources", []):
            content = fetched.get(src["url"], "[not fetched]")
            sources_block.append(f"\n#### Source: {src['name']} ({src['url']})\n{content}")

    sources_text = "\n".join(sources_block)

    # Build the new archive <li> that will be prepended
    new_archive_li = f'      <li><a href="./archive/{date_iso}.html">{date_long}</a></li>'
    if existing_archive_li:
        updated_archive_li = new_archive_li + "\n      " + existing_archive_li
    else:
        updated_archive_li = new_archive_li

    return f"""You are generating a personal daily news digest in HTML.

Today: {date_long}, {time_cest}
Lookback window: {lookback_h} hours
Max items per source: {max_per_source}
Max items per topic: {max_per_topic}
Deduplicate near-identical stories: {dedupe}

Below is raw text scraped from each configured source. Pick the most newsworthy items
published within the lookback window. Apply any per-topic filters. Skip fetched-failed
sources silently. Skip press releases, op-eds without substance, and near-duplicates.

{sources_text}

---

Output EXACTLY two files using this format:

FILE: archive/{date_iso}.html
<full HTML here>
END_FILE

FILE: index.html
<full HTML here>
END_FILE

Both files use this shared CSS (embed verbatim inside <style>):

{CSS}

The archive page (archive/{date_iso}.html) structure:
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Digest — {date_long}</title>
<style>
[CSS here]
</style>
</head>
<body>
<header>
  <h1>Daily Digest</h1>
  <div class="date">{date_long}</div>
</header>

[one <section> per topic that has items]

<footer>
  Generated by Claude &middot; {date_long}, {time_cest} &middot; <a href="../index.html">&larr; Back to latest</a>
</footer>
</body>
</html>

The index.html structure is identical to the archive page EXCEPT:
1. Title is just "Daily Digest" (no date)
2. Append this archive section before </body>:

<div class="archive-section">
  <h2>Archive</h2>
  <ul class="archive-list">
{updated_archive_li}
  </ul>
</div>

3. Footer reads:
  Generated by Claude &middot; {date_long}, {time_cest} &middot; Sources configured in <a href="./sources.yaml">sources.yaml</a>

Each topic section format:
<section>
  <h2>Topic Name</h2>
  <p class="window-note">Items from the last {lookback_h} hours</p>

  <div class="item">
    <p class="item-title"><a href="URL">Title</a></p>
    <p class="item-meta">Source &middot; Date</p>
    <p class="item-summary">2-4 sentence factual summary.</p>
  </div>

</section>

Rules:
- Omit a topic section entirely if no qualifying items were found.
- For the Jobs topic: list postings factually, no editorialising; omit the window-note.
- Use HTML entities for special characters (&mdash; &middot; &rsquo; &ndash; &nbsp; etc).
- Do not add any text outside the two FILE blocks.
"""


def parse_file_blocks(text: str) -> dict[str, str]:
    files = {}
    pattern = re.compile(r'^FILE:\s*(\S+?)\r?\n(.*?)^END_FILE', re.MULTILINE | re.DOTALL)
    for m in pattern.finditer(text):
        files[m.group(1).strip()] = m.group(2)
    return files


def git_commit_push(date_iso: str) -> None:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "github-actions[bot]",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "github-actions[bot]",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    })
    subprocess.run(
        ["git", "add", "index.html", f"archive/{date_iso}.html"],
        cwd=REPO_ROOT, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"digest: {date_iso}"],
        cwd=REPO_ROOT, env=env, check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=REPO_ROOT, check=True,
    )


def main() -> None:
    config = yaml.safe_load(SOURCES_FILE.read_text())
    now = datetime.now(CEST)
    date_iso = now.strftime("%Y-%m-%d")

    print(f"Generating digest for {date_iso} ({now.strftime('%H:%M CEST')})")

    # Fetch all source URLs
    fetched: dict[str, str] = {}
    with httpx.Client(headers={"User-Agent": "DailyDigestBot/1.0"}, verify=True) as client:
        for topic in config.get("topics", []):
            for src in topic.get("sources", []):
                url = src["url"]
                if url not in fetched:
                    print(f"  fetching {src['name']} …")
                    fetched[url] = fetch_source(url, client)

    existing_archive_li = read_existing_archive_list()
    prompt = build_prompt(config, fetched, now, existing_archive_li)

    print("Calling Claude …")
    ai = anthropic.Anthropic()
    with ai.messages.stream(
        model="claude-opus-4-8",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        print("ERROR: Claude returned no text block", file=sys.stderr)
        sys.exit(1)

    files = parse_file_blocks(text)
    archive_key = f"archive/{date_iso}.html"
    if archive_key not in files or "index.html" not in files:
        print(f"ERROR: expected {archive_key} and index.html, got: {list(files)}", file=sys.stderr)
        print("--- raw response ---")
        print(text[:2000])
        sys.exit(1)

    ARCHIVE_DIR.mkdir(exist_ok=True)
    (ARCHIVE_DIR / f"{date_iso}.html").write_text(files[archive_key], encoding="utf-8")
    INDEX_FILE.write_text(files["index.html"], encoding="utf-8")

    print(f"Written archive/{date_iso}.html and index.html")
    git_commit_push(date_iso)
    print("Committed and pushed to main.")


if __name__ == "__main__":
    main()
