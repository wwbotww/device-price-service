from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(slots=True)
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    parent: HtmlNode | None = None
    children: list[HtmlNode] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.text())
        return " ".join(part.strip() for part in parts if part.strip())

    def has_class(self, class_name: str) -> bool:
        return class_name in self.attrs.get("class", "").split()

    def find_all(self, predicate: Callable[[HtmlNode], bool]) -> list[HtmlNode]:
        found = [self] if predicate(self) else []
        for child in self.children:
            found.extend(child.find_all(predicate))
        return found

    def first(self, predicate: Callable[[HtmlNode], bool]) -> HtmlNode | None:
        if predicate(self):
            return self
        for child in self.children:
            found = child.first(predicate)
            if found is not None:
                return found
        return None


class _TreeParser(HTMLParser):
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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._stack[-1]
        node = HtmlNode(
            tag=tag.lower(),
            attrs={key.lower(): value or "" for key, value in attrs},
            parent=parent,
        )
        parent.children.append(node)
        if node.tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text_parts.append(data)


def parse_html(value: str | bytes) -> HtmlNode:
    source = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root
