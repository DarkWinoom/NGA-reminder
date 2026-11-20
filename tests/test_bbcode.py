"""Tests for the BBCode -> plain text summary helper."""

from __future__ import annotations

from nga_reminder.nga.bbcode import to_single_line, to_text


def test_plain_text_is_unchanged():
    assert to_text("白嫖个教练") == "白嫖个教练"


def test_bold_tags_are_unwrapped():
    assert to_text("[b]粗体[/b]内容") == "粗体内容"


def test_quote_blocks_are_dropped():
    assert to_text("正文[quote]引用内容[/quote]尾巴") == "正文 尾巴"


def test_images_become_a_placeholder():
    assert to_text("看图[img]这里") == "看图[图片]这里"


def test_smileys_are_removed():
    assert to_text("[s:ac:2]你好") == "你好"


def test_url_tags_keep_the_label():
    assert to_text("[url=https://x.test]点我[/url]") == "点我 (https://x.test)"


def test_html_breaks_become_newlines():
    assert to_text("第一行<br>第二行") == "第一行\n第二行"


def test_entities_are_decoded():
    assert to_text("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_truncation_adds_an_ellipsis():
    out = to_text("x" * 50, max_length=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_single_line_collapses_newlines():
    assert to_single_line("第一行\n第二行") == "第一行 第二行"


def test_single_line_truncates():
    out = to_single_line("y" * 200, max_length=30)
    assert len(out) == 30
    assert out.endswith("…")


def test_empty_input():
    assert to_text("") == ""
    assert to_single_line("") == ""
