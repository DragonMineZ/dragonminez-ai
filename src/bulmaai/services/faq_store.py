
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FaqItem:
    title: str
    keywords: list[str]
    answer: str


class FaqStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.items: list[FaqItem] = []

    def load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.items = [FaqItem(**item) for item in raw]

    def search(self, query: str) -> FaqItem | None:
        q = query.strip().lower()
        if not q:
            return None

        best: tuple[int, FaqItem] | None = None
        for item in self.items:
            score = 0
            hay = (item.title + " " + " ".join(item.keywords)).lower()
            if q in hay:
                score += 3
            for kw in item.keywords:
                if kw.lower() in q:
                    score += 2

            if score > 0 and (best is None or score > best[0]):
                best = (score, item)

        return best[1] if best else None