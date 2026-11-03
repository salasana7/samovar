#!/usr/bin/env python3
"""
Samovar collector template.

A collector script fetches posts from a source and outputs them as JSON lines
to stdout. The samovar harness calls this script and ingests the output.

Output format: one JSON object per line, with these fields:

    Required:
        post_id         Unique identifier for the post (string)
        source          Source name, matching the key in samovar.yaml (string)
        text            Post content (string)

    Recommended:
        source_language ISO 639-1 language code, e.g. "ru" (string)
        url             Direct URL to the post (string)
        thread_url      URL to the containing thread — used by the investigate
                        agent to fetch context via curl (string)
        source_ts       Original post timestamp, ISO 8601 (string)

    Optional:
        metadata        Any additional source-specific data (object)

Example output line:
    {"post_id": "12345", "source": "my_source", "source_language": "ru", "text": "...", "url": "https://...", "thread_url": "https://...", "source_ts": "2026-03-15T10:00:00Z"}

Usage:
    1. Copy this template to sources/<your_source>/crawl.py
    2. Implement your collection logic
    3. Configure in samovar.yaml:
         sources:
           my_source:
             script: sources/my_source/crawl.py
             args: ["--flag", "value"]
    4. Run: samovar collect my_source
"""

import argparse
import json
import sys


def collect(args):
    """Replace this with your collection logic."""
    # Example: fetch from an API, read from a database, parse HTML, etc.
    #
    # For each post found, emit a JSON line:
    #
    #   post = {
    #       "post_id": "unique_id",
    #       "source": "my_source",
    #       "source_language": "ru",
    #       "text": "post content here",
    #       "url": "https://example.com/post/unique_id",
    #       "thread_url": "https://example.com/thread/123",
    #       "source_ts": "2026-03-15T10:00:00Z",
    #   }
    #   emit(post)

    pass  # TODO: implement


def emit(post: dict):
    """Write a single post as a JSON line to stdout."""
    print(json.dumps(post, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collector for my_source")
    # Add your arguments here
    args = parser.parse_args()
    collect(args)
