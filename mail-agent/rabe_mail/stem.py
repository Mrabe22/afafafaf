"""De 'stem' van Rabe: stijlgids, handtekening en eerdere antwoorden als voorbeelden (BM25-zoeken)."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

STOPWOORDEN = set("""
de het een en of in op aan van voor met bij dat die dit deze is zijn was waren wordt worden ik je jij u uw we wij
ze zij hij mijn ons onze er niet geen ook nog wel maar als dan om te tot naar uit over door kan kunnen zou zouden
heb hebt heeft hebben wil wilt willen graag even mvg groet vriendelijke beste hallo hoi goedemorgen goedemiddag
der die das ein eine und oder in auf an von für mit bei dass ist sind war ich sie wir nicht kein auch noch aber
wenn dann zu nach aus über durch kann können haben habe hat bitte danke grüßen freundlichen
the a an and or of to for with at is are was i you we it not this that be have has please thanks regards
""".split())


def tokens(tekst: str) -> list[str]:
    tekst = unicodedata.normalize("NFKD", tekst.lower())
    tekst = "".join(c for c in tekst if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z0-9]{2,}", tekst) if t not in STOPWOORDEN]


@dataclass
class Voorbeeld:
    vraag: str
    antwoord: str
    onderwerp: str = ""
    datum: str = ""
    taal: str = ""
    soort: str = "klant"


class Voorbeelden:
    """Eenvoudige BM25-index over de klantvragen uit eerdere mailwisselingen."""

    def __init__(self, voorbeelden: list[Voorbeeld], k1: float = 1.4, b: float = 0.75):
        self.items = voorbeelden
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(v.onderwerp + " " + v.vraag)) for v in voorbeelden]
        self.lengtes = [sum(d.values()) for d in self.docs]
        self.gem = (sum(self.lengtes) / len(self.lengtes)) if self.lengtes else 0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def laad(cls, pad: Path, alleen_klant: bool = True) -> "Voorbeelden":
        items = []
        if pad.exists():
            for regel in pad.read_text(encoding="utf-8").splitlines():
                if not regel.strip():
                    continue
                d = json.loads(regel)
                if alleen_klant and d.get("soort", "klant") != "klant":
                    continue
                items.append(Voorbeeld(vraag=d.get("vraag", ""), antwoord=d.get("antwoord", ""),
                                       onderwerp=d.get("onderwerp", ""), datum=d.get("datum", ""),
                                       taal=d.get("taal", ""), soort=d.get("soort", "klant")))
        return cls(items)

    def __len__(self):
        return len(self.items)

    def zoek(self, tekst: str, k: int = 3) -> list[Voorbeeld]:
        if not self.items:
            return []
        q = set(tokens(tekst))
        scores = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = d.get(t)
                if not f:
                    continue
                noemer = f + self.k1 * (1 - self.b + self.b * self.lengtes[i] / (self.gem or 1))
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / noemer
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [self.items[i] for _, i in scores[:k]]


def formatteer_voorbeelden(voorbeelden: list[Voorbeeld], max_tekens: int = 1200) -> str:
    if not voorbeelden:
        return "(geen vergelijkbare eerdere mails gevonden)"
    delen = []
    for n, v in enumerate(voorbeelden, 1):
        vraag = v.vraag.strip()[:max_tekens]
        antwoord = v.antwoord.strip()[:max_tekens]
        delen.append(f"<voorbeeld nummer=\"{n}\" datum=\"{v.datum}\">\n<klant>\n{vraag}\n</klant>\n"
                     f"<rabe_antwoordde>\n{antwoord}\n</rabe_antwoordde>\n</voorbeeld>")
    return "\n\n".join(delen)


STANDAARD_HANDTEKENING = "Rabe Trading\n0547 231015\nrabetrading.com"


def laad_tekst(pad: Path, standaard: str = "") -> str:
    return pad.read_text(encoding="utf-8").strip() if pad.exists() else standaard
