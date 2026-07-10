from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT_DIR = Path(__file__).resolve().parent.parent

INLINE_LINK_RE = re.compile(r"!?\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
REF_LINK_RE = re.compile(r"^\[[^\]]+\]:\s+(\S+)", re.MULTILINE)
HTML_ANCHOR_RE = re.compile(r"<a\s+[^>]*(?:id|name)=[\"']([^\"']+)[\"']", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown_files() -> list[Path]:
    files = sorted(ROOT_DIR.glob("*.md"))
    files.extend(sorted((ROOT_DIR / "docs").rglob("*.md")))
    return files


def _strip_code_fences(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return "\n".join(lines)


def _slugify_heading(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.strip().lower()
    chars: list[str] = []
    for char in text:
        if char.isalnum() or char in {" ", "-", "_"}:
            chars.append(char)
    slug = re.sub(r"\s+", "-", "".join(chars).strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _anchors_for(path: Path) -> set[str]:
    text = _strip_code_fences(path.read_text(encoding="utf-8"))
    anchors: set[str] = {""}
    anchors.update(match.group(1) for match in HTML_ANCHOR_RE.finditer(text))
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        slug = _slugify_heading(match.group(2))
        if not slug:
            continue
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links_in(path: Path) -> list[str]:
    text = _strip_code_fences(path.read_text(encoding="utf-8"))
    links = [match.group(1) for match in INLINE_LINK_RE.finditer(text)]
    links.extend(match.group(1) for match in REF_LINK_RE.finditer(text))
    return links


def _is_external(link: str) -> bool:
    parsed = urlsplit(link)
    return bool(parsed.scheme) or link.startswith("//")


class DocumentationLinkTests(unittest.TestCase):
    def test_markdown_links_and_anchors_resolve(self) -> None:
        failures: list[str] = []
        anchor_cache: dict[Path, set[str]] = {}

        for source in _markdown_files():
            for raw_link in _links_in(source):
                link = raw_link.strip().strip("<>")
                if not link or _is_external(link):
                    continue
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc:
                    continue

                target_path = source if not parsed.path else (source.parent / unquote(parsed.path)).resolve()
                try:
                    target_path.relative_to(ROOT_DIR)
                except ValueError:
                    failures.append(f"{source.relative_to(ROOT_DIR)} -> {raw_link} escapes repository")
                    continue

                if target_path.is_dir():
                    target_path = target_path / "README.md"
                if not target_path.exists():
                    failures.append(f"{source.relative_to(ROOT_DIR)} -> {raw_link} missing target")
                    continue
                if parsed.fragment:
                    anchors = anchor_cache.setdefault(target_path, _anchors_for(target_path))
                    if unquote(parsed.fragment).lower() not in anchors:
                        failures.append(
                            f"{source.relative_to(ROOT_DIR)} -> {raw_link} missing anchor "
                            f"in {target_path.relative_to(ROOT_DIR)}"
                        )

        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
