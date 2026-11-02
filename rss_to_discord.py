#!/usr/bin/env python3
"""Poll an RSS/Atom feed and forward entries with matching titles to Discord.

Settings are read from config.json (see the repository copy). The Discord
webhook is the only secret and is read from the DISCORD_WEBHOOK_URL
environment variable.
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

USER_AGENT = "chuy-rss-ds/1.0"
HTTP_TIMEOUT = 30
DISCORD_COLOR = 0x5865F2
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_TITLE_LIMIT = 256
DISCORD_AUTHOR_LIMIT = 256
DISCORD_FOOTER_LIMIT = 1024
DISCORD_MESSAGE_CHAR_LIMIT = 6000
DISCORD_MAX_EMBEDS = 10
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_BLOCK_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_SPACE_RE = re.compile(r"\s+")
_MD_ESCAPE_RE = re.compile(r"([\\*_~`|>#\[\]])")
_MD_LANG_RE = re.compile(r"(?:language|lang)-([A-Za-z0-9_+-]+)")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|https?://[^\s<>()]+")


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def config_str(raw: dict, key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise SystemExit(f"config: {key} must be a string")
    return value.strip()


def config_int(
    raw: dict, key: str, default: int, minimum: int = 0, maximum: int | None = None
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"config: {key} must be an integer")
    if value < minimum:
        raise SystemExit(f"config: {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SystemExit(f"config: {key} must be <= {maximum}")
    return value


_CONFIG_KEYS = {
    "feed_url",
    "title_regex",
    "title_regex_case_sensitive",
    "state_file",
    "max_sends_per_run",
    "description_limit",
    "state_max_entries",
}


@dataclass(frozen=True)
class Config:
    feed_url: str
    title_pattern: re.Pattern[str]
    webhook_url: str
    state_path: Path
    max_sends: int
    description_limit: int
    state_max_entries: int
    dry_run: bool

    @classmethod
    def load(cls, path: Path, webhook_url: str) -> "Config":
        if not path.exists():
            raise SystemExit(f"config file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read config file {path}: {exc}")
        if not isinstance(raw, dict):
            raise SystemExit(f"config file {path} must contain a JSON object")

        unknown = sorted(set(raw) - _CONFIG_KEYS)
        if unknown:
            raise SystemExit(f"unknown key(s) in {path}: {', '.join(unknown)}")

        feed_url = config_str(raw, "feed_url")
        if not feed_url:
            raise SystemExit("config: feed_url is required")
        if not feed_url.startswith(("http://", "https://")):
            raise SystemExit("config: feed_url must start with http:// or https://")

        raw_regex = raw.get("title_regex")
        if not isinstance(raw_regex, str) or raw_regex == "":
            raise SystemExit("config: title_regex is required and must be a non-empty string")
        case_sensitive = raw.get("title_regex_case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise SystemExit("config: title_regex_case_sensitive must be true or false")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(raw_regex, flags)
        except re.error as exc:
            raise SystemExit(f"config: invalid title_regex: {exc}")

        state_file = config_str(raw, "state_file", "state.json") or "state.json"

        return cls(
            feed_url=feed_url,
            title_pattern=pattern,
            webhook_url=webhook_url,
            state_path=path.parent / state_file,
            max_sends=config_int(raw, "max_sends_per_run", 10, minimum=0),
            description_limit=config_int(
                raw,
                "description_limit",
                DISCORD_DESCRIPTION_LIMIT,
                minimum=0,
                maximum=DISCORD_DESCRIPTION_LIMIT,
            ),
            state_max_entries=config_int(raw, "state_max_entries", 5000, minimum=1),
            dry_run=False,
        )


@dataclass
class Entry:
    key: str
    title: str
    link: str
    summary: str
    published: datetime | None = None
    author: str = ""


def fetch(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": (
                        "application/rss+xml, application/atom+xml, "
                        "application/xml, text/xml;q=0.9, */*;q=0.8"
                    ),
                },
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                body = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return body
        except Exception as exc:  # network, TLS, HTTP, gzip
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch feed: {last_error}")


def clean_html(value: str) -> str:
    value = _BLOCK_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    value = html.unescape(value)
    return _SPACE_RE.sub(" ", value).strip()


def escape_markdown(value: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\\1", value)


def language_from_class(value: str) -> str:
    match = _MD_LANG_RE.search(value or "")
    return match.group(1).lower() if match else ""


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


_INLINE_FORMATS = {
    "strong": ("**", "**"),
    "b": ("**", "**"),
    "em": ("*", "*"),
    "i": ("*", "*"),
    "u": ("__", "__"),
    "s": ("~~", "~~"),
    "strike": ("~~", "~~"),
    "del": ("~~", "~~"),
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "title", "meta"}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "aside",
    "figure",
    "figcaption",
    "dl",
    "dt",
    "dd",
    "address",
    "tr",
}


class _MarkdownRenderer:
    def __init__(self, base_url: str = "", images: list[str] | None = None) -> None:
        self.base_url = base_url
        self.images = images if images is not None else []
        self.out = ""
        self.in_pre = False
        self.in_li = False
        self.list_stack: list[list] = []

    def render(self, root: _Node) -> str:
        self._render_children(root)
        text = self.out.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    def _write(self, value: str) -> None:
        self.out += value

    def _newlines(self, count: int) -> None:
        if not self.out:
            return
        self.out = self.out.rstrip(" \t")
        trailing = 0
        for char in reversed(self.out):
            if char == "\n":
                trailing += 1
            else:
                break
        if trailing < count:
            self.out += "\n" * (count - trailing)

    def _capture_children(self, node: _Node) -> str:
        saved = self.out
        self.out = ""
        try:
            self._render_children(node)
            return self.out
        finally:
            self.out = saved

    def _capture_pre(self, node: _Node) -> str:
        saved = self.in_pre
        self.in_pre = True
        try:
            return self._capture_children(node)
        finally:
            self.in_pre = saved

    def _render_children(self, node: _Node) -> None:
        for child in node.children:
            self._render_node(child)

    def _render_node(self, node: _Node | str) -> None:
        if isinstance(node, str):
            self._render_text(node)
            return

        tag = node.tag
        if tag in _SKIP_TAGS:
            return
        if self.in_pre:
            self._render_children(node)
            return
        if tag == "br":
            self.out = self.out.rstrip(" \t")
            self._write("\n")
            return
        if tag == "hr":
            self._newlines(2)
            self._write("──────────")
            self._newlines(2)
            return
        if tag == "table":
            self._render_table(node)
            return
        if tag in ("h1", "h2", "h3"):
            self._render_heading(node, int(tag[1]))
            return
        if tag in ("h4", "h5", "h6"):
            self._render_bold_heading(node)
            return
        if tag in ("ul", "ol"):
            self._render_list(node, tag == "ol")
            return
        if tag == "blockquote":
            self._render_blockquote(node)
            return
        if tag == "pre":
            self._render_pre(node)
            return
        if tag == "code":
            self._render_inline_code(node)
            return
        if tag == "a":
            self._render_link(node)
            return
        if tag == "img":
            self._render_image(node)
            return
        if tag in _INLINE_FORMATS:
            self._render_inline_format(node, *_INLINE_FORMATS[tag])
            return
        if tag in _BLOCK_TAGS:
            self._render_block(node)
            return
        self._render_children(node)

    def _render_text(self, text: str) -> None:
        if not text:
            return
        if self.in_pre:
            self._write(text)
            return
        collapsed = _SPACE_RE.sub(" ", text)
        if not collapsed.strip():
            if collapsed and self.out and not self.out.endswith((" ", "\n")):
                self._write(" ")
            return
        if not self.out or self.out.endswith((" ", "\n")):
            collapsed = collapsed.lstrip(" ")
        if collapsed:
            self._write(escape_markdown(collapsed))

    def _render_block(self, node: _Node) -> None:
        if self.in_li:
            self._render_children(node)
            return
        inner = self._capture_children(node).strip("\n")
        if not inner.strip():
            return
        self._newlines(2)
        self._write(inner)
        self._newlines(2)

    def _render_inline_format(self, node: _Node, prefix: str, suffix: str) -> None:
        inner = self._capture_children(node).strip()
        if not inner:
            return
        self._write(prefix + inner + suffix)

    def _render_inline_code(self, node: _Node) -> None:
        inner = _SPACE_RE.sub(" ", self._capture_pre(node)).strip()
        if not inner:
            return
        fence = "``" if "`" in inner else "`"
        self._write(f"{fence}{inner}{fence}")

    def _render_pre(self, node: _Node) -> None:
        inner = self._capture_pre(node).strip("\n")
        if not inner.strip():
            return
        language = ""
        for child in node.children:
            if isinstance(child, _Node) and child.tag == "code":
                language = language_from_class(child.attrs.get("class", ""))
                break
        fence = "````" if "```" in inner else "```"
        self._newlines(2)
        self._write(f"{fence}{language}\n{inner}\n{fence}")
        self._newlines(2)

    def _render_heading(self, node: _Node, level: int) -> None:
        inner = _SPACE_RE.sub(" ", self._capture_children(node)).strip()
        if not inner:
            return
        self._newlines(2)
        self._write("#" * level + " " + inner)
        self._newlines(2)

    def _render_bold_heading(self, node: _Node) -> None:
        inner = _SPACE_RE.sub(" ", self._capture_children(node)).strip()
        if not inner:
            return
        self._newlines(2)
        self._write("**" + inner + "**")
        self._newlines(2)

    def _render_list(self, node: _Node, ordered: bool) -> None:
        self._newlines(1)
        self.list_stack.append([ordered, 0])
        depth = len(self.list_stack) - 1
        indent = "  " * depth
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag in ("ul", "ol"):
                self._render_list(child, child.tag == "ol")
            elif child.tag == "li":
                self.list_stack[-1][1] += 1
                if ordered:
                    marker = f"{self.list_stack[-1][1]}. "
                else:
                    marker = "- "
                self._newlines(1)
                self._write(indent + marker)
                saved = self.in_li
                self.in_li = True
                try:
                    self._render_children(child)
                finally:
                    self.in_li = saved
        self.list_stack.pop()
        self._newlines(1)

    def _render_blockquote(self, node: _Node) -> None:
        inner = self._capture_children(node).strip("\n")
        if not inner.strip():
            return
        lines = []
        for line in inner.split("\n"):
            lines.append("> " + line if line.strip() else ">")
        self._newlines(2)
        self._write("\n".join(lines))
        self._newlines(2)

    def _render_link(self, node: _Node) -> None:
        href = (node.attrs.get("href") or "").strip()
        inner = self._capture_children(node).strip()
        if href and self.base_url:
            href = urljoin(self.base_url, href)
        if not href.startswith(("http://", "https://")):
            if inner:
                self._write(inner)
            return
        href = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        if not inner or inner.replace("\\", "").startswith(("http://", "https://")):
            self._write(href)
            return
        self._write(f"[{inner}]({href})")

    def _render_image(self, node: _Node) -> None:
        src = (node.attrs.get("src") or "").strip()
        if src and self.base_url:
            src = urljoin(self.base_url, src)
        alt = _SPACE_RE.sub(" ", node.attrs.get("alt") or "").strip()
        if src.startswith(("http://", "https://")):
            src = src.replace(" ", "%20")
            if src not in self.images:
                self.images.append(src)
        elif alt:
            self._write(escape_markdown(alt))

    def _render_table(self, node: _Node) -> None:
        rows: list[str] = []
        for row in self._iter_nodes(node, "tr"):
            cells = [
                _SPACE_RE.sub(" ", self._capture_children(cell)).strip()
                for cell in row.children
                if isinstance(cell, _Node) and cell.tag in ("td", "th")
            ]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append(" | ".join(cells))
        if not rows:
            return
        self._newlines(2)
        self._write("\n".join(rows))
        self._newlines(2)

    @staticmethod
    def _iter_nodes(node: _Node, tag: str):
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == tag:
                yield child
            else:
                yield from _MarkdownRenderer._iter_nodes(child, tag)


def html_to_markdown(
    value: str, base_url: str = "", images: list[str] | None = None
) -> str:
    if not value or not value.strip():
        return ""
    try:
        builder = _TreeBuilder()
        builder.feed(value)
        builder.close()
        return _MarkdownRenderer(base_url, images).render(builder.root)
    except Exception:
        return clean_html(value)


def parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def text_of(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def child(element: ET.Element, name: str) -> ET.Element | None:
    for candidate in element:
        if local_name(candidate.tag) == name:
            return candidate
    return None


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [candidate for candidate in element if local_name(candidate.tag) == name]


def entry_link(element: ET.Element) -> str:
    links = children(element, "link")
    if not links:
        return ""
    for link in links:
        rel = (link.get("rel") or "alternate").lower()
        href = (link.get("href") or "").strip()
        if rel == "alternate" and href:
            return href
    for link in links:
        href = (link.get("href") or "").strip()
        if href:
            return href
    return text_of(links[0])


def entry_published(element: ET.Element) -> datetime | None:
    for name in ("pubdate", "published", "updated", "issued", "date", "created"):
        published = parse_date(text_of(child(element, name)))
        if published is not None:
            return published
    return None


def entry_summary(element: ET.Element) -> str:
    for name in ("description", "summary", "content", "encoded"):
        summary = text_of(child(element, name))
        if summary:
            return summary
    return ""


def clean_author(value: str) -> str:
    value = " ".join(value.split())
    match = re.match(r"^[^\s<>()]+@[^\s<>()]*\s*\((.+)\)$", value)
    if match:
        return match.group(1).strip()
    match = re.match(r"^(.+?)\s*<[^<>]+>$", value)
    if match:
        return match.group(1).strip()
    return value


def entry_author(element: ET.Element) -> str:
    author = child(element, "author")
    if author is not None:
        name = text_of(child(author, "name"))
        if name:
            return clean_author(name)
        email = text_of(child(author, "email"))
        if email:
            return clean_author(email)
        value = text_of(author)
        if value:
            return clean_author(value)
    creator = text_of(child(element, "creator"))
    if creator:
        return clean_author(creator)
    return ""


def parse_feed(data: bytes) -> tuple[str, list[Entry]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid feed XML: {exc}")

    feed_title = ""
    if local_name(root.tag) == "feed":
        feed_title = text_of(child(root, "title"))
    else:
        channel = child(root, "channel")
        if channel is not None:
            feed_title = text_of(child(channel, "title"))

    entries: list[Entry] = []
    seen: set[str] = set()
    for node in root.iter():
        if local_name(node.tag) not in ("item", "entry"):
            continue
        title = clean_html(text_of(child(node, "title")))
        link = entry_link(node)
        raw_id = text_of(child(node, "guid")) or text_of(child(node, "id")) or link
        published = entry_published(node)
        if not title and not link and not raw_id:
            continue
        if not raw_id:
            raw_id = f"{title}|{published.isoformat() if published else ''}"
        if raw_id in seen:
            continue
        seen.add(raw_id)
        entries.append(
            Entry(
                key=raw_id,
                title=title or link or "(untitled)",
                link=link,
                summary=entry_summary(node),
                published=published,
                author=entry_author(node),
            )
        )
    return feed_title.strip(), entries


def oldest_first(entries: list[Entry]) -> list[Entry]:
    if not entries:
        return []
    dated = [entry for entry in entries if entry.published is not None]
    undated = [entry for entry in entries if entry.published is None]
    dated.sort(key=lambda entry: entry.published or datetime.min.replace(tzinfo=timezone.utc))
    return dated + list(reversed(undated))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "sent": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read state file {path}: {exc}")
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid state file {path}: expected a JSON object")
    sent = raw.setdefault("sent", {})
    if not isinstance(sent, dict):
        raise RuntimeError(f"invalid state file {path}: 'sent' must be an object")
    return raw


def prune_state(state: dict, max_entries: int) -> None:
    sent = state.get("sent") or {}
    if len(sent) <= max_entries:
        return
    ordered = sorted(
        sent.items(),
        key=lambda item: item[1].get("sent_at", "") if isinstance(item[1], dict) else "",
    )
    for key, _ in ordered[: len(sent) - max_entries]:
        del sent[key]


def save_state(path: Path, state: dict) -> None:
    state["version"] = 1
    state["updated_at"] = utc_now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _markdown_link_cut(text: str, cut: int) -> int:
    for match in _MD_LINK_RE.finditer(text):
        if match.start() >= cut:
            break
        if match.end() > cut:
            return match.start()
    return cut


def _split_point(text: str, budget: int) -> int:
    window = text[:budget]
    minimum = budget // 3
    for separator in ("\n\n", "\n", " "):
        index = window.rfind(separator)
        if index < minimum:
            continue
        cut = _markdown_link_cut(text, index + len(separator))
        if cut >= minimum:
            return cut
    cut = _markdown_link_cut(text, budget)
    return cut if cut > 0 else budget


def _has_open_code_fence(text: str) -> bool:
    return text.count("```") % 2 == 1


def split_markdown(value: str, limit: int) -> list[str]:
    text = value.strip()
    if not text:
        return []
    limit = limit if 0 < limit <= DISCORD_DESCRIPTION_LIMIT else DISCORD_DESCRIPTION_LIMIT
    chunks: list[str] = []
    reopen_fence = False
    while text:
        prefix = "```\n" if reopen_fence else ""
        budget = max(1, limit - len(prefix))
        if len(text) <= budget:
            piece, text = text, ""
        else:
            cut = _split_point(text, budget)
            piece, text = text[:cut].rstrip(), text[cut:].lstrip("\n")
            if not piece:
                piece, text = text[:budget], text[budget:]
        reopen_fence = _has_open_code_fence(prefix + piece)
        if reopen_fence:
            closer = "\n```"
            room = limit - len(prefix) - len(closer)
            if room > 0:
                piece = piece[:room].rstrip() + closer
            else:
                piece = piece[: max(1, limit - len(prefix))]
        chunk = (prefix + piece).strip("\n")
        if chunk:
            chunks.append(chunk)
    return chunks


def _page_footer(feed_title: str, page: int, total: int) -> str:
    label = f"(Página {page}/{total})" if total > 1 else ""
    if feed_title and label:
        suffix = " " + label
        return (feed_title[: max(0, DISCORD_FOOTER_LIMIT - len(suffix))] + suffix)[
            :DISCORD_FOOTER_LIMIT
        ]
    if feed_title:
        return feed_title[:DISCORD_FOOTER_LIMIT]
    return label[:DISCORD_FOOTER_LIMIT]


def build_embeds(entry: Entry, feed_title: str, description_limit: int) -> list[dict]:
    images: list[str] = []
    description = html_to_markdown(entry.summary, entry.link, images)
    pages = split_markdown(description, description_limit)
    total = max(1, len(pages))
    embeds: list[dict] = []

    for page_number, page in enumerate(pages or [""], 1):
        embed: dict = {"color": DISCORD_COLOR}
        if page_number == 1:
            embed["title"] = entry.title[:DISCORD_TITLE_LIMIT] or "(untitled)"
            if entry.author:
                embed["author"] = {"name": entry.author[:DISCORD_AUTHOR_LIMIT]}
            if is_url(entry.link):
                embed["url"] = entry.link[:2048]
            if entry.published is not None:
                embed["timestamp"] = entry.published.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                )
            if images:
                embed["image"] = {"url": images[0]}
        footer = _page_footer(feed_title, page_number, total)
        if footer:
            embed["footer"] = {"text": footer}
        if page:
            embed["description"] = page
        embeds.append(embed)

    for image in images[1:]:
        embeds.append({"color": DISCORD_COLOR, "image": {"url": image}})
    return embeds


def _embed_char_count(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    author = embed.get("author") or {}
    footer = embed.get("footer") or {}
    total += len(author.get("name", "")) + len(footer.get("text", ""))
    for field in embed.get("fields") or []:
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total


def _chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for embed in embeds:
        size = _embed_char_count(embed)
        if size > DISCORD_MESSAGE_CHAR_LIMIT:
            raise RuntimeError("discord embed exceeds the 6000 character message limit")
        if current and (
            len(current) >= DISCORD_MAX_EMBEDS
            or current_chars + size > DISCORD_MESSAGE_CHAR_LIMIT
        ):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(embed)
        current_chars += size
    if current:
        groups.append(current)
    return groups


def post_discord(webhook_url: str, embeds: list[dict]) -> None:
    for group in _chunk_embeds(embeds):
        _post_embeds(webhook_url, group)


def _post_embeds(webhook_url: str, embeds: list[dict]) -> None:
    payload = json.dumps(
        {
            "embeds": embeds,
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")

    last_error = "unknown error"
    for attempt in range(1, 6):
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                response.read()
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    reset_after = response.headers.get("X-RateLimit-Reset-After")
                    if reset_after:
                        try:
                            time.sleep(min(float(reset_after) + 0.25, 60.0))
                        except ValueError:
                            pass
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            last_error = f"HTTP {exc.code}: {body[:300]}"
            if exc.code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(json.loads(body).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(min(max(retry_after, 0.0), 60.0) + 0.5)
                continue
            if 500 <= exc.code < 600 and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"discord webhook rejected the message: {last_error}")
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
            if attempt < 5:
                time.sleep(2**attempt)
                continue
        except OSError as exc:
            last_error = str(exc)
            if attempt < 5:
                time.sleep(2**attempt)
                continue
    raise RuntimeError(f"discord webhook failed after retries: {last_error}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll an RSS/Atom feed and forward matching entries to Discord."
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--state", help="override state_file from config")
    parser.add_argument("--max-sends", type=int, help="override max_sends_per_run (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="log matches without sending")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        raise SystemExit("missing required environment variable: DISCORD_WEBHOOK_URL")
    if not webhook_url.startswith(("http://", "https://")):
        raise SystemExit("DISCORD_WEBHOOK_URL must start with http:// or https://")

    config = Config.load(Path(args.config), webhook_url)

    overrides: dict = {}
    if args.state:
        overrides["state_path"] = Path(args.state)
    if args.max_sends is not None:
        if args.max_sends < 0:
            raise SystemExit("--max-sends must be >= 0")
        overrides["max_sends"] = args.max_sends
    if args.dry_run:
        overrides["dry_run"] = True
    if overrides:
        config = replace(config, **overrides)

    state = load_state(config.state_path)
    sent: dict = state["sent"]

    feed_title, entries = parse_feed(fetch(config.feed_url))
    matched = [
        entry
        for entry in entries
        if config.title_pattern.search(entry.title) and entry.key not in sent
    ]
    to_send = oldest_first(matched)
    if config.max_sends > 0:
        to_send = to_send[: config.max_sends]

    log(
        f"entries={len(entries)} new_matches={len(matched)} "
        f"sending={len(to_send)} dry_run={config.dry_run}"
    )

    sent_count = 0
    for entry in to_send:
        if config.dry_run:
            log(f"[dry-run] would send id={entry.key!r} title={entry.title!r}")
            sent_count += 1
            continue
        embeds = build_embeds(entry, feed_title, config.description_limit)
        post_discord(config.webhook_url, embeds)
        sent[entry.key] = {
            "title": entry.title,
            "link": entry.link,
            "sent_at": utc_now_iso(),
        }
        sent_count += 1
        prune_state(state, config.state_max_entries)
        save_state(config.state_path, state)
        log(f"sent id={entry.key!r} title={entry.title!r}")

    if config.dry_run:
        log(f"dry run complete: would_send={sent_count} registry={len(sent)}")
        return 0

    if sent_count == 0:
        if config.state_path.exists():
            log(f"nothing to send registry={len(sent)}")
            return 0
        save_state(config.state_path, state)
        log("created empty state file")
        return 0

    log(f"done sent={sent_count} registry={len(sent)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
