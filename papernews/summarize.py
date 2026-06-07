from __future__ import annotations

import re
from typing import Sequence

from . import llm

_SYSTEM = (
    "You write a 2-sentence summary of a piece of content for a daily digest.\n"
    "\n"
    "HARD RULES:\n"
    "- ALWAYS output a summary. NEVER refuse. NEVER ask the user a question. NEVER reply in the first person. NEVER comment on the suitability of the content.\n"
    "- The piece may be a news article, blog post, Show HN, discussion thread, fiction, satire, opinion, product launch, release notes, paper, or anything else. Summarize whatever it is. Fiction → summarize the plot. Show HN → say what the project does. Opinion → state the position.\n"
    "- Be terse and factual. State what the piece is about and the main point or takeaway. No filler. No 'this article discusses', 'the author argues', 'the piece explores'.\n"
    "- Hard cap: 40 words across the 2 sentences.\n"
    "- Output language: ENGLISH, regardless of the source language. Translate if needed.\n"
    "- Output ONLY the summary text. No preamble, no quotes, no markdown, no questions, no meta-commentary.\n"
    "\n"
    "BATCH MODE:\n"
    "- The user may send multiple articles in one message, each wrapped in a numbered <article id=\"N\"> block.\n"
    "- For each article, emit one summary on its own line, prefixed with `N. ` (the article's id and a period).\n"
    "- Output ONLY those summary lines, in the same order as the input. No surrounding text."
)

_MODEL = "claude-haiku-4-5"  # reference only; model selection lives in llm.py
_MAX_CHARS = 4000


def summarize(title: str, text: str) -> str:
    return summarize_batch([(title, text)])[0]


def summarize_batch(items: Sequence[tuple[str, str]]) -> list[str]:
    """Summarize many (title, body) pairs in a single Anthropic call.
    Returns one summary per input, in order. Falls back to empty string for
    any item the model failed to label correctly."""
    if not items:
        return []

    parts = []
    for i, (title, text) in enumerate(items):
        snippet = (text or "")[:_MAX_CHARS]
        parts.append(
            f"<article id=\"{i}\">\n<title>{title}</title>\n<body>\n{snippet}\n</body>\n</article>"
        )
    user_msg = "\n\n".join(parts)

    text = llm.chat(_SYSTEM, user_msg, max_tokens=300 * len(items)).strip()

    out = [""] * len(items)
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s*[.)]\s*(.*\S)\s*$", line)
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < len(items):
            out[idx] = m.group(2)
    return out
