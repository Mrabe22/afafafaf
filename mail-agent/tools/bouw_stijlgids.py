"""Maakt data/stijlgids.md: hoe Rabe schrijft en welke adviezen hij steeds geeft, afgeleid uit zijn eigen mails.

Leest data/voorbeelden.jsonl (gemaakt met export_naar_voorbeelden.py), kiest een gevarieerde steekproef van
recente klantmails en laat Claude daar een stijlgids van maken. Eenmalig, kost ongeveer $0,20 tot $0,50.
Lees het resultaat daarna na: het is een gewoon tekstbestand dat je zelf kunt aanpassen.

Gebruik (vanuit de map mail-agent):
    python tools/bouw_stijlgids.py [--aantal 120]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from rabe_mail.claude import FALLBACK_BETA, Verbruik  # noqa: E402
from rabe_mail.config import DATA, Config  # noqa: E402

OPDRACHT = """Hieronder staan echte mailwisselingen van Rabe Trading (een Nederlandse familiewebshop in beregening,
watertechniek en tuin- en landbouwmachines): telkens wat de klant schreef en wat Rabe antwoordde.

Schrijf een stijlgids in het Nederlands waarmee een andere schrijver precies zo kan antwoorden als Rabe, zodat
een klant het verschil niet merkt. Baseer alles op wat je in zijn antwoorden ziet, niet op algemene adviezen
over klantenservice. Citeer zijn eigen formuleringen letterlijk waar dat helpt.

Gebruik deze kopjes:
1. Toon en houding
2. Aanhef en opening (per taal, met letterlijke voorbeelden; wanneer "u" en wanneer "je")
3. Zinsbouw en lengte (hoe lang zijn zijn mails echt, hoeveel alinea's, lijstjes of niet)
4. Typische woorden en zinnen die hij vaak gebruikt
5. Hoe hij nee zegt of slecht nieuws brengt
6. Zijn adviezen: welk advies geeft hij steeds terug, per onderwerp (bijvoorbeeld pompkeuze, filters, slangen,
   machines, levering). Dit is het belangrijkste deel: beschrijf de inhoud van zijn adviezen en zijn redenering.
7. Hoe hij een mail afsluit (zonder de handtekening zelf)
8. Wat hij nooit doet
9. Duits en Engels: wat is daar anders

Regels: neem geen namen, e-mailadressen, telefoonnummers of andere gegevens van klanten op. Noem geen prijzen,
levertijden of voorraad als vaste feiten (die veranderen). Gebruik geen gedachtestreepjes. Maximaal ongeveer
1.500 woorden. Geef alleen de stijlgids, zonder inleiding."""


def kies(voorbeelden: list[dict], aantal: int, max_tekens: int = 140_000) -> list[dict]:
    klant = [v for v in voorbeelden if v.get("soort", "klant") == "klant"]
    recent = klant[: aantal * 2]
    random.Random(7).shuffle(recent)
    gekozen, totaal = [], 0
    for v in sorted(recent[:aantal], key=lambda x: x["datum"], reverse=True):
        stuk = len(v["vraag"][:1500]) + len(v["antwoord"][:2000])
        if totaal + stuk > max_tekens:
            break
        gekozen.append(v)
        totaal += stuk
    return gekozen


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--aantal", type=int, default=120)
    p.add_argument("--in", dest="invoer", default=str(DATA / "voorbeelden.jsonl"))
    p.add_argument("--uit", default=str(DATA / "stijlgids.md"))
    args = p.parse_args(argv)

    cfg = Config.uit_omgeving()
    pad = Path(args.invoer)
    if not pad.exists():
        print(f"{pad} bestaat nog niet. Draai eerst tools/export_naar_voorbeelden.py")
        return 2
    voorbeelden = [json.loads(r) for r in pad.read_text(encoding="utf-8").splitlines() if r.strip()]
    gekozen = kies(voorbeelden, args.aantal)
    if len(gekozen) < 10:
        print(f"Te weinig klantvoorbeelden ({len(gekozen)}) voor een betrouwbare stijlgids.")
        return 2
    blokken = "\n\n".join(
        f"<wisseling datum=\"{v['datum']}\" taal=\"{v['taal']}\">\n<klant>\n{v['vraag'][:1500]}\n</klant>\n"
        f"<rabe>\n{v['antwoord'][:2000]}\n</rabe>\n</wisseling>" for v in gekozen)

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=cfg.model,
        max_tokens=64000,
        output_config={"effort": "high"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        messages=[{"role": "user", "content": f"{blokken}\n\n{OPDRACHT}"}],
    ) as stream:
        antwoord = stream.get_final_message()
    if antwoord.stop_reason == "refusal":
        print("Claude weigerde deze aanvraag; probeer het met een kleinere --aantal.")
        return 1
    tekst = "\n".join(b.text for b in antwoord.content if b.type == "text").strip()
    verbruik = Verbruik()
    verbruik.tel(cfg.model, antwoord.usage)
    Path(args.uit).write_text(tekst + "\n", encoding="utf-8")
    print(f"Stijlgids op basis van {len(gekozen)} mailwisselingen geschreven naar {args.uit} "
          f"(kosten ${verbruik.dollars:.2f}). Lees hem na en pas aan wat niet klopt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
