"""Regression tests for tex_body's handling of literal `$` versus LaTeX math.

A literal dollar sign inside inline code — `$HOME`, `$(pwd)`, a shell snippet —
used to be mis-parsed as the opening of inline math: the math scanner matched
forward to the next `$` and swallowed the prose in between, emitting broken
LaTeX that fails xelatex with::

    ! Paragraph ended before \\text@command was complete.

These tests pin the fix while making sure genuine inline math still renders.

Run them with::

    python -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

from papernews.render import tex_body


class InlineCodeDollarTests(unittest.TestCase):
    def test_dollar_in_inline_code_is_escaped_not_mathified(self):
        body = "Every user has the same `$HOME` and the same `$PATH` on the box."
        out = tex_body(body)
        self.assertIn(r"\texttt{\$HOME}", out)
        self.assertIn(r"\texttt{\$PATH}", out)
        # The prose between the two code spans must survive intact.
        self.assertIn("and the same", out)

    def test_two_inline_code_dollars_do_not_form_a_math_span(self):
        # The classic break: `$HOME` ... `$HOME`. Previously the region between
        # the two dollars became one giant (broken) math expression.
        body = "Both `ubuntu` and `debian` share the same `$HOME` and dotfiles."
        out = tex_body(body)
        self.assertIn(r"\texttt{\$HOME}", out)
        self.assertIn(r"\texttt{ubuntu}", out)
        self.assertIn(r"\texttt{debian}", out)

    def test_real_inline_math_still_passes_through(self):
        body = "The area scales like $r^2$ as the radius grows."
        out = tex_body(body)
        self.assertIn("$r^2$", out)

    def test_display_math_still_passes_through(self):
        body = "Euler:\n\n$$e^{i\\pi} + 1 = 0$$"
        out = tex_body(body)
        self.assertIn(r"\[e^{i\pi} + 1 = 0\]", out)

    def test_stray_dollar_in_prose_is_escaped_and_does_not_span_paragraphs(self):
        body = "It cost $5 yesterday.\n\nToday it is cheaper."
        out = tex_body(body)
        self.assertIn(r"\$5", out)
        self.assertIn("Today it is cheaper.", out)


if __name__ == "__main__":
    unittest.main()
