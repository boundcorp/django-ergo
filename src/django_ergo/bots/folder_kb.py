"""A knowledge base that is just a folder of Markdown files.

    kb/
      preferences/diet.md
      household.md

Each file is an article. An optional first ``# Heading`` is its title. Search
is lexical (word matches in the title and body), so it needs no index or
embeddings, and the folder can live in a bot's git repo, where the bot edits
articles and proposes them like any other change (see the bot_management
plugin). Files are read fresh on every call.

The root article, ``index.md`` (or ``README.md``) at the top of the folder,
is what the bot should always know; the ergo_kb plugin puts it in context on
every turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool

MAX_ARTICLES = 2000
MAX_ARTICLE_CHARS = 50_000
SNIPPET_CHARS = 300
ROOT_ARTICLES = ("index.md", "README.md")
_WORD = re.compile(r"[\w'-]{3,}", re.UNICODE)


@dataclass
class Article:
    path: str
    title: str
    body: str


class FolderKB:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def articles(self) -> list[Article]:
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(self.root.rglob("*.md"))[:MAX_ARTICLES]:
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")[:MAX_ARTICLE_CHARS]
            relative = path.relative_to(self.root).as_posix()
            title = relative
            first = text.lstrip().splitlines()[0] if text.strip() else ""
            if first.startswith("# "):
                title = first[2:].strip()
            found.append(Article(relative, title, text))
        return found

    def root_article(self) -> Article | None:
        for name in ROOT_ARTICLES:
            path = self.root / name
            if path.is_file():
                text = path.read_text(errors="replace")[:MAX_ARTICLE_CHARS]
                first = text.lstrip().splitlines()[0] if text.strip() else ""
                title = first[2:].strip() if first.startswith("# ") else name
                return Article(name, title, text)
        return None

    def search(self, query: str, limit: int = 5) -> list[tuple[Article, int]]:
        terms = {w.lower() for w in _WORD.findall(query)}
        if not terms:
            return []
        scored = []
        for article in self.articles():
            title = article.title.lower()
            body = article.body.lower()
            score = sum(body.count(t) + 3 * title.count(t) for t in terms)
            if score:
                scored.append((article, score))
        scored.sort(key=lambda item: (-item[1], item[0].path))
        return scored[:limit]

    def read(self, path: str) -> Article:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or target.suffix != ".md":
            msg = f"{path} is not an article in this knowledge base"
            raise ValueError(msg)
        if not target.is_file():
            msg = f"No article {path}"
            raise ValueError(msg)
        for article in self.articles():
            if article.path == target.relative_to(self.root).as_posix():
                return article
        msg = f"No article {path}"
        raise ValueError(msg)

    def render_results(self, query: str, limit: int = 5) -> str:
        results = self.search(query, limit)
        if not results:
            return "No matching articles."
        blocks = []
        for article, _score in results:
            snippet = " ".join(article.body.split())[:SNIPPET_CHARS]
            blocks.append(f"### {article.title} ({article.path})\n{snippet}")
        return "\n\n".join(blocks)

    def toolkit(self, ctx=None, *, prefix: str = "kb") -> FunctionToolkit:
        kb = self

        @bot_tool(name=f"{prefix}_search")
        def search(query: str, top_k: int = 5) -> str:
            """Search the knowledge base articles by keywords."""
            return kb.render_results(query, top_k)

        @bot_tool(name=f"{prefix}_read")
        def read(path: str) -> str:
            """Read one knowledge base article by its path."""
            article = kb.read(path)
            return f"# {article.path}\n\n{article.body}"

        @bot_tool(name=f"{prefix}_list")
        def list_articles() -> str:
            """List every knowledge base article with its title."""
            articles = kb.articles()
            if not articles:
                return "The knowledge base is empty."
            return "\n".join(f"{a.path}: {a.title}" for a in articles)

        tools: list[BotTool] = [fn.__bot_tool__ for fn in (search, read, list_articles)]
        return FunctionToolkit(tools, ctx)
