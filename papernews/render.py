from __future__ import annotations

import html as _html
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
from markupsafe import Markup


_TEX_REPLACE = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex_escape(s) -> str:
    if s is None:
        return ""
    return "".join(_TEX_REPLACE.get(c, c) for c in str(s))


def tex_paragraphs(text: str) -> str:
    # trafilatura's plain-text output separates paragraphs with a single
    # newline; any run of \n is a paragraph boundary.
    parts = re.split(r"\n+", text or "")
    paras = [p.strip() for p in parts if p.strip()]
    return "\n\n".join(tex_escape(p) for p in paras)


def tex_url(url: str) -> str:
    # \href{} is mostly raw-tolerant but breaks on %, #, \, and unbalanced
    # braces. Escape just the dangerous ones.
    if not url:
        return ""
    return (
        url.replace("\\", r"\\")
        .replace("%", r"\%")
        .replace("#", r"\#")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+\-]*\s*\n?(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")

# Math delimiters: $$...$$, \[...\], $...$, \(...\). Order matters — double
# dollars first so they win over single dollars, and bracket forms before
# round forms for the same reason.
_MATH_RE = re.compile(
    r"\$\$(?P<dd>.+?)\$\$"
    r"|\\\[(?P<br>.+?)\\\]"
    r"|(?<![\\$])\$(?P<sd>[^$\n][^$]*?)\$(?!\d)"
    r"|\\\((?P<pr>.+?)\\\)",
    re.DOTALL,
)


def _tex_escape_code(s: str) -> str:
    return (
        s.replace("\\", r"\textbackslash{}")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("$", r"\$")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


def _render_code_block(code: str) -> str:
    lines = code.rstrip("\n").split("\n")
    rendered = []
    for ln in lines:
        m = re.match(r"^[ \t]*", ln)
        leading = m.group(0).replace("\t", "    ")
        rest = ln[len(m.group(0)):]
        line_tex = ("~" * len(leading)) + _tex_escape_code(rest)
        rendered.append(f"\\mbox{{}}{line_tex}\\par")
    inner = "\n".join(rendered)
    return (
        "\n\\par\\smallskip\n"
        "{\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{0pt}"
        "\\ttfamily\\footnotesize\\raggedright\n"
        + inner
        + "\n}\n\\par\\smallskip\n"
    )


def _process_inline(text: str) -> str:
    parts = _INLINE_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(tex_escape(part))
        else:
            out.append("\\texttt{" + _tex_escape_code(part) + "}")
    return "".join(out)


def _stash_math(text: str) -> tuple[str, list[str]]:
    """Replace $..$ / $$..$$ / \\(..\\) / \\[..\\] with placeholders.
    Returns (text_with_placeholders, list_of_LaTeX_math_to_inject)."""
    bits: list[str] = []

    def stash(m: re.Match) -> str:
        if m.group("dd") is not None:
            bits.append(f"\\[{m.group('dd').strip()}\\]")
        elif m.group("br") is not None:
            bits.append(f"\\[{m.group('br').strip()}\\]")
        elif m.group("sd") is not None:
            bits.append(f"${m.group('sd').strip()}$")
        else:  # pr
            bits.append(f"${m.group('pr').strip()}$")
        return f"\x00MB{len(bits) - 1}\x00"

    return _MATH_RE.sub(stash, text), bits


_LEADING_DATE_RE = re.compile(
    r"^\s*("
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{2,4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{2,4}"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r")\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_leading_date_line(text: str) -> str:
    """If the body starts with a date-only line (the publication date the
    author wrote at the top of their post), drop it — we already show the
    publication date in the article header."""
    lines = text.lstrip("\n").split("\n")
    while lines and _LEADING_DATE_RE.fullmatch(lines[0].strip()):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


def tex_body(text: str) -> str:
    """Render an article body that may contain markdown-fenced code blocks,
    inline backtick code, and LaTeX math expressions ($..$, $$..$$, etc.).
    Prose flows as paragraphs; code becomes monospace blocks; math passes
    through to LaTeX math mode untouched."""
    if not text:
        return ""
    text = _strip_leading_date_line(text)

    # 1) Stash fenced code blocks first.
    blocks: list[str] = []

    def stash_code(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00CB{len(blocks) - 1}\x00"

    stashed = _FENCE_RE.sub(stash_code, text)

    # 2) Stash math expressions so escaping doesn't munge backslashes/braces.
    stashed, math_bits = _stash_math(stashed)

    paras = (
        re.split(r"\n\s*\n+", stashed.strip())
        if "\n\n" in stashed
        else re.split(r"\n+", stashed.strip())
    )
    out = []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        m = re.fullmatch(r"\x00CB(\d+)\x00", p)
        if m:
            out.append(_render_code_block(blocks[int(m.group(1))]))
            continue

        # Expand inline code blocks (rare — usually their own paragraph).
        def expand_code(mm: re.Match) -> str:
            return _render_code_block(blocks[int(mm.group(1))])

        p = re.sub(r"\x00CB(\d+)\x00", expand_code, p)

        # Process prose: handle inline backtick code + tex-escape the rest,
        # WITHOUT touching math placeholders.
        rendered = _process_inline(p)

        # Re-inject math placeholders as raw LaTeX (they were escaped to
        # \x00MB{N}\x00 → \textbackslash{}x00MB... — restore.
        def expand_math(mm: re.Match) -> str:
            return math_bits[int(mm.group(1))]

        # The placeholder may have been mangled by tex_escape; recover both
        # the raw and any escaped form.
        rendered = re.sub(r"\x00MB(\d+)\x00", expand_math, rendered)
        out.append(rendered)
    return "\n\n".join(out)


def _env(tpl_dir: Path) -> jinja2.Environment:
    env = jinja2.Environment(
        block_start_string="((*",
        block_end_string="*))",
        variable_start_string="(((",
        variable_end_string=")))",
        comment_start_string="((=",
        comment_end_string="=))",
        loader=jinja2.FileSystemLoader(str(tpl_dir)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tex"] = tex_escape
    env.filters["texpar"] = tex_paragraphs
    env.filters["texurl"] = tex_url
    env.filters["body"] = tex_body
    return env


def build_pdf(
    date: str,
    articles: list[dict],
    out_dir: Path,
    decorations: dict | None = None,
) -> Path:
    tpl_dir = Path(__file__).parent
    env = _env(tpl_dir)
    tpl = env.get_template("template.tex.j2")
    tex_source = tpl.render(date=date, articles=articles, decorations=decorations or {})

    workdir = out_dir / ".build"
    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{date}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    # Run twice so hyperref's page references resolve.
    for _ in range(2):
        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory",
                str(workdir),
                str(tex_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout[-4000:])
            sys.stderr.write(result.stderr[-2000:])
            raise RuntimeError(f"xelatex failed (exit {result.returncode})")

    pdf_src = workdir / f"{date}.pdf"
    pdf_dst = out_dir / f"{date}.pdf"
    shutil.copy(pdf_src, pdf_dst)
    return pdf_dst


# ---------------------------------------------------------------------------
# HTML / EPUB rendering
# ---------------------------------------------------------------------------

_HTML_INLINE_RE = re.compile(r"`([^`\n]+)`")


def _process_html_inline(text: str) -> str:
    """Convert inline `code` spans to <code>. Assumes text is already HTML-escaped."""
    parts = _HTML_INLINE_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(part)
        else:
            out.append(f"<code>{_html.escape(part)}</code>")
    return "".join(out)


def html_body(text: str) -> Markup:
    """Render an article body to safe HTML for the EPUB template.
    Fenced code → <pre><code>, inline code → <code>, paragraphs → <p>,
    dash lists → <ul><li>, math delimiters pass through untouched."""
    if not text:
        return Markup("")
    text = _strip_leading_date_line(text)

    blocks: list[str] = []

    def stash_code(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00CB{len(blocks) - 1}\x00"

    stashed = _FENCE_RE.sub(stash_code, text)

    paras = (
        re.split(r"\n\s*\n+", stashed.strip())
        if "\n\n" in stashed
        else re.split(r"\n+", stashed.strip())
    )

    out: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            out.append("<ul>" + "".join(list_items) + "</ul>")
            list_items.clear()

    for p in paras:
        p = p.strip()
        if not p:
            continue

        m = re.fullmatch(r"\x00CB(\d+)\x00", p)
        if m:
            flush_list()
            out.append(f"<pre><code>{_html.escape(blocks[int(m.group(1))])}</code></pre>")
            continue

        if p.startswith("- "):
            list_items.append(f"<li>{_process_html_inline(_html.escape(p[2:]))}</li>")
            continue

        flush_list()
        out.append(f"<p>{_process_html_inline(_html.escape(p))}</p>")

    flush_list()
    return Markup("\n".join(out))


def _html_env(tpl_dir: Path) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(tpl_dir)),
        autoescape=True,
    )
    env.filters["html_body"] = html_body
    return env


def build_epub(
    date: str,
    articles: list[dict],
    out_dir: Path,
    decorations: dict | None = None,
) -> Path:
    tpl_dir = Path(__file__).parent
    env = _html_env(tpl_dir)
    tpl = env.get_template("template.html.j2")
    html_source = tpl.render(date=date, articles=articles, decorations=decorations or {})

    workdir = out_dir / ".build"
    workdir.mkdir(parents=True, exist_ok=True)
    html_path = workdir / f"{date}.html"
    html_path.write_text(html_source, encoding="utf-8")

    epub_src = workdir / f"{date}.epub"
    result = subprocess.run(
        [
            "pandoc",
            "--from=html+tex_math_dollars",
            "--to=epub3",
            "--mathml",
            "--toc",
            "--toc-depth=2",
            f"--metadata=title=papernews — {date}",
            f"--metadata=date={date}",
            "-o", str(epub_src),
            str(html_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
        raise RuntimeError(f"pandoc failed (exit {result.returncode})")

    epub_dst = out_dir / f"{date}.epub"
    shutil.copy(epub_src, epub_dst)
    return epub_dst
