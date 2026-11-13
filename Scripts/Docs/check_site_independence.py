#!/usr/bin/env python3
"""Reject published links that escape the standalone Understanding Unreal site.

The check parses only real ``href`` and ``src`` attributes under ``Docs``. It
does not inspect prose: comparative mentions of Ardashir, migration history,
and Epic source citations are allowed.
"""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

FORBIDDEN_GITHUB_REPOS = frozenset({"ardashir", "ardashader"})
FORBIDDEN_PAGES_SITES = frozenset({"ardashir", "ardashader"})
FORBIDDEN_LOCAL_DIRS = (
    "ardabackend",
    "ardardg",
    "ardascene",
    "ardatrace",
    "essays",
)


def _normalized_segments(path: str) -> list[str]:
    """Return decoded, dot-normalized, non-empty path segments."""
    decoded = unquote(path or "").replace("\\", "/")
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return segments


def _repo_key(segment: str) -> str:
    segment = segment.casefold()
    return segment[:-4] if segment.endswith(".git") else segment


def _forbidden_external(url: str) -> str | None:
    """Return a reason when an absolute or protocol-relative URL is forbidden."""
    stripped = url.strip()
    if stripped.startswith("//"):
        stripped = "https:" + stripped
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return None
    if parts.scheme.casefold() not in ("http", "https") or not parts.netloc:
        return None

    host = (parts.hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    segments = [segment.casefold() for segment in _normalized_segments(parts.path)]

    if host == "github.com":
        if (
            len(segments) >= 2
            and segments[0] == "dylanwaken"
            and _repo_key(segments[1]) in FORBIDDEN_GITHUB_REPOS
        ):
            return "forbidden GitHub repository DylanWaken/" + segments[1]

    if host == "dylanwaken.github.io":
        if segments and segments[0] in FORBIDDEN_PAGES_SITES:
            return "forbidden published site dylanwaken.github.io/" + segments[0]

    return None


def _forbidden_local(url: str, source_path: Path, docs_root: Path) -> str | None:
    """Return a reason when a local destination lands in an old Ardashir tree."""
    stripped = url.strip()
    if not stripped or stripped.startswith("#"):
        return None

    try:
        parts = urlsplit(stripped)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None

    raw_path = parts.path
    if not raw_path or raw_path == ".":
        return None

    decoded = unquote(raw_path).replace("\\", "/")
    raw_segments = _normalized_segments(decoded)
    lowered = [segment.casefold() for segment in raw_segments]

    # The explicit repository-relative forms such as ``Docs/ArdaBackend``.
    if len(lowered) >= 2 and lowered[0] == "docs":
        if lowered[1] in FORBIDDEN_LOCAL_DIRS:
            return "forbidden local path Docs/" + raw_segments[1]

    # Site-root forms resolve against Docs; relative forms resolve against the
    # containing page's directory.
    try:
        if decoded.startswith("/"):
            resolved = (docs_root / decoded.lstrip("/")).resolve()
        else:
            resolved = (source_path.parent / decoded).resolve()
        relative = resolved.relative_to(docs_root.resolve())
    except (OSError, ValueError):
        relative = None
    if relative is not None and relative.parts:
        first = relative.parts[0].casefold()
        if first in FORBIDDEN_LOCAL_DIRS:
            return "forbidden local path Docs/" + relative.parts[0]

    return None


def _forbidden_reason(url: str, source_path: Path, docs_root: Path) -> str | None:
    return _forbidden_external(url) or _forbidden_local(url, source_path, docs_root)


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.casefold() in ("href", "src") and value is not None:
                self.links.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(attrs)


def check(docs_root: Path) -> tuple[list[Path], list[tuple[Path, str, str]]]:
    docs_root = docs_root.resolve()
    pages = sorted(docs_root.rglob("*.html"))
    offenders: list[tuple[Path, str, str]] = []

    for page in pages:
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SystemExit(f"error: cannot read {page}: {exc}") from exc

        parser = _LinkCollector()
        parser.feed(text)
        parser.close()

        for link in parser.links:
            reason = _forbidden_reason(link, page, docs_root)
            if reason is not None:
                offenders.append((page, link, reason))

    return pages, offenders


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Reject cross-site links in the standalone Docs site."
    )
    parser.add_argument(
        "docs_root",
        nargs="?",
        type=Path,
        default=repo_root / "Docs",
        help="Docs directory to scan (default: the Docs directory beside this script).",
    )
    args = parser.parse_args(argv)

    docs_root = args.docs_root.resolve()
    if not docs_root.is_dir():
        print(f"error: docs directory not found: {docs_root}", file=sys.stderr)
        return 2

    pages, offenders = check(docs_root)
    if not pages:
        print(f"error: no HTML files found under {docs_root}", file=sys.stderr)
        return 2

    if offenders:
        for page, link, reason in offenders:
            try:
                display = page.relative_to(docs_root.parent).as_posix()
            except ValueError:
                display = page.as_posix()
            print(f"{display}: {link} ({reason})")
        print(
            f"Site independence check failed: {len(offenders)} forbidden link(s) "
            f"in {len(pages)} HTML file(s).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Site independence check passed: {len(pages)} HTML file(s) under "
        f"{docs_root} contain no links to Ardashir."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
