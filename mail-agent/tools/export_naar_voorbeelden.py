"""Maakt van een (grote) mailexport een klein bestand met voorbeelden van hoe Rabe klanten antwoordt.

Het script draait op je eigen pc en stuurt niets naar buiten. Het leest de export (zip, map, .mbox of .eml),
zoekt alle mails die JIJ verstuurd hebt, koppelt ze aan de mail waarop je antwoordde, laat bijlagen weg en
schrijft het resultaat naar data/voorbeelden.jsonl (meestal een paar MB in plaats van honderden).

Gebruik (vanuit de map mail-agent):
    python tools/export_naar_voorbeelden.py "C:\\Users\\Barma\\Downloads\\mijndomein-export.zip"
    python tools/export_naar_voorbeelden.py --imap            rechtstreeks uit de mailbox (Verzonden + INBOX)
Opties:
    --adres info@rabetrading.com   je eigen adres(sen); standaard MAIL_ADRES + EIGEN_ADRESSEN uit .env
    --sinds 2024-01-01             alleen mails vanaf deze datum
    --uit data/voorbeelden.jsonl   uitvoerbestand
"""
from __future__ import annotations

import argparse
import json
import mailbox
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rabe_mail.berichten import Mail, citaat_deel, is_automatisch, ontleed, zonder_citaat  # noqa: E402
from rabe_mail.config import DATA, Config  # noqa: E402

AFSLUITERS = re.compile(
    r"^\s*(met vriendelijke groet(en)?|vriendelijke groet(en)?|met hartelijke groet|hartelijke groet(en)?|"
    r"groet(en|jes)?|mvg|m\.v\.g\.|mit freundlichen gr(ü|ue)(ß|ss)en|freundliche gr(ü|ue)(ß|ss)e|beste gr(ü|ue)(ß|ss)e|"
    r"viele gr(ü|ue)(ß|ss)e|kind regards|best regards|regards)\b[,!.]?\s*$",
    re.I,
)
MARKTPLAATS = ("bol.com", "amazon.", "marktplaats.nl", "kaufland", "ebay.")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
TELEFOON = re.compile(r"(?<![\w])(\+?\d[\d ()\-]{8,}\d)(?![\w])")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}\s?(?:[A-Z0-9]{4}\s?){2,7}[A-Z0-9]{1,4}\b")
EIGEN_NUMMERS = {"0547231015", "+31547231015", "31547231015"}
EIGEN_IBAN = "NL67RABO0387122842"


def taal_van(tekst: str) -> str:
    woorden = re.findall(r"[a-zäöüß]+", tekst.lower())
    nl = sum(w in {"de", "het", "een", "en", "ik", "je", "u", "wij", "niet", "voor", "met", "graag", "bedankt"} for w in woorden)
    de = sum(w in {"der", "die", "das", "und", "ich", "sie", "wir", "nicht", "für", "mit", "bitte", "danke", "ihr"} for w in woorden)
    en = sum(w in {"the", "and", "you", "we", "not", "for", "with", "please", "thanks", "your"} for w in woorden)
    return max((nl, "nl"), (de, "de"), (en, "en"))[1]


def maskeer(tekst: str, eigen: list[str]) -> str:
    def mail_vervang(m):
        return m.group(0) if m.group(0).lower() in eigen else "[e-mail]"

    def tel_vervang(m):
        cijfers = re.sub(r"[^\d+]", "", m.group(1))
        if cijfers in EIGEN_NUMMERS or len(re.sub(r"\D", "", cijfers)) < 9:
            return m.group(0)
        return "[telefoon]"

    def iban_vervang(m):
        return m.group(0) if m.group(0).replace(" ", "") == EIGEN_IBAN else "[iban]"

    tekst = EMAIL.sub(mail_vervang, tekst)
    tekst = IBAN.sub(iban_vervang, tekst)
    # Eigen IBAN even afschermen, anders ziet de telefoonregel de cijfers ervan als nummer.
    eigen_iban = [m.group(0) for m in IBAN.finditer(tekst) if m.group(0).replace(" ", "") == EIGEN_IBAN]
    for i, waarde in enumerate(eigen_iban):
        tekst = tekst.replace(waarde, f"\x00IBAN{i}\x00", 1)
    tekst = TELEFOON.sub(tel_vervang, tekst)
    for i, waarde in enumerate(eigen_iban):
        tekst = tekst.replace(f"\x00IBAN{i}\x00", waarde, 1)
    return tekst


def splits_handtekening(tekst: str) -> tuple[str, str]:
    """Knipt de afsluitgroet + handtekening eraf. Geeft (kern, handtekening)."""
    regels = tekst.rstrip().splitlines()
    for i in range(len(regels) - 1, max(-1, len(regels) - 25), -1):
        if AFSLUITERS.match(regels[i]):
            return "\n".join(regels[:i]).rstrip(), "\n".join(regels[i:]).strip()
    return tekst.strip(), ""


# -- bronnen lezen --------------------------------------------------------------------------------
def berichten_uit_pad(pad: Path):
    """Levert ruwe mails (bytes) uit een zip, map, mbox- of eml-bestand."""
    if pad.is_dir():
        for p in sorted(pad.rglob("*")):
            if p.is_file():
                yield from berichten_uit_bestand(p)
    else:
        yield from berichten_uit_bestand(pad)


def _is_mbox(kop: bytes) -> bool:
    return kop.startswith(b"From ") and b"\n" in kop[:2000]


def _is_mail(kop: bytes) -> bool:
    return bool(re.search(rb"^(Return-Path|Received|From|Message-ID|Date|Subject|MIME-Version|Delivered-To):",
                          kop[:4000], re.I | re.M))


def berichten_uit_bestand(p: Path):
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                with z.open(info) as f:
                    kop = f.read(4096)
                if info.filename.lower().endswith(".zip") or kop.startswith(b"PK\x03\x04"):
                    with tempfile.TemporaryDirectory() as tmp:
                        doel = Path(tmp) / "binnen.zip"
                        with z.open(info) as bron, open(doel, "wb") as uit:
                            shutil.copyfileobj(bron, uit)
                        yield from berichten_uit_bestand(doel)
                elif _is_mbox(kop):
                    with tempfile.TemporaryDirectory() as tmp:
                        doel = Path(tmp) / "map.mbox"
                        with z.open(info) as bron, open(doel, "wb") as uit:
                            shutil.copyfileobj(bron, uit)
                        yield from _uit_mbox(doel)
                elif _is_mail(kop):
                    with z.open(info) as f:
                        yield f.read()
        return
    with open(p, "rb") as f:
        kop = f.read(4096)
    if _is_mbox(kop):
        yield from _uit_mbox(p)
    elif _is_mail(kop):
        yield p.read_bytes()


def _uit_mbox(p: Path):
    mb = mailbox.mbox(str(p), create=False)
    try:
        for sleutel in mb.iterkeys():
            try:
                yield mb.get_bytes(sleutel)
            except Exception:
                continue
    finally:
        mb.close()


# -- verwerken ------------------------------------------------------------------------------------
class Verzamelaar:
    def __init__(self, eigen: list[str], sinds: date | None, leverancier_domeinen: list[str]):
        self.eigen = [a.lower() for a in eigen]
        self.sinds = sinds
        self.lev = [d.lower() for d in leverancier_domeinen]
        self.inkomend: dict[str, dict] = {}
        self.verzonden: list[dict] = []
        self.tellers = Counter()

    def voeg_toe(self, mail: Mail) -> None:
        self.tellers["gelezen"] += 1
        if self.sinds and mail.datum and mail.datum.date() < self.sinds:
            return
        # Shopify-contactformulieren komen soms van je eigen adres met de klant als Reply-To: dat is inkomend.
        if mail.van_adres in self.eigen and not mail.antwoord_adres:
            self.tellers["van jou"] += 1
            self.verzonden.append({
                "mid": mail.message_id, "in_reply_to": mail.kopregels.get("In-Reply-To", "").strip(),
                "references": mail.references.split(), "aan": mail.aan, "onderwerp": mail.onderwerp,
                "datum": mail.datum.isoformat() if mail.datum else "", "tekst": mail.tekst[:12000]})
        else:
            if is_automatisch(mail):
                self.tellers["automatisch"] += 1
                return
            if mail.message_id:
                self.inkomend[mail.message_id] = {"tekst": zonder_citaat(mail.tekst)[:5000],
                                                  "onderwerp": mail.onderwerp}

    def soort(self, aan: list[str]) -> str:
        ontvangers = [a for a in aan if a not in self.eigen]
        if not ontvangers:
            return "intern"
        domein = ontvangers[0].split("@")[-1]
        if any(domein == d or domein.endswith("." + d) for d in self.lev):
            return "leverancier"
        if any(m in domein for m in MARKTPLAATS):
            return "marktplaats"
        return "klant"

    def koppel(self) -> tuple[list[dict], Counter]:
        paren, handtekeningen, gezien = [], Counter(), set()
        for v in self.verzonden:
            ouder = self.inkomend.get(v["in_reply_to"])
            if not ouder:
                for ref in reversed(v["references"]):
                    if ref in self.inkomend:
                        ouder = self.inkomend[ref]
                        break
            eigen_tekst = zonder_citaat(v["tekst"])
            kern, handtekening = splits_handtekening(eigen_tekst)
            if handtekening:
                handtekeningen[handtekening] += 1
            if ouder:
                vraag = ouder["tekst"]
            else:
                vraag = zonder_citaat(citaat_deel(v["tekst"]))
            if len(kern) < 30 or len(vraag) < 15:
                self.tellers["te kort of geen vraag"] += 1
                continue
            sleutel = (vraag[:200], kern[:200])
            if sleutel in gezien:
                continue
            gezien.add(sleutel)
            paren.append({
                "datum": v["datum"][:10], "onderwerp": v["onderwerp"], "soort": self.soort(v["aan"]),
                "taal": taal_van(kern), "vraag": maskeer(vraag, self.eigen), "antwoord": maskeer(kern, self.eigen)})
        paren.sort(key=lambda x: x["datum"], reverse=True)
        return paren, handtekeningen


def lees_imap(cfg: Config, verz: Verzamelaar, sinds: date | None) -> None:
    from rabe_mail.mailbox import Mailbox

    with Mailbox(cfg.imap_host, cfg.imap_poort, cfg.mail_adres, cfg.mail_wachtwoord) as mb:
        for n, mail in enumerate(mb.verzonden_mails(sinds), 1):
            verz.voeg_toe(mail)
            if n % 200 == 0:
                print(f"  {n} verzonden mails gelezen...")
        for n, mail in enumerate(mb.inbox_mails(sinds), 1):
            verz.voeg_toe(mail)
            if n % 500 == 0:
                print(f"  {n} inbox-mails gelezen...")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pad", nargs="?", help="mailexport: zip, map, .mbox of .eml")
    p.add_argument("--imap", action="store_true", help="rechtstreeks uit de mailbox lezen")
    p.add_argument("--adres", action="append", default=[], help="eigen e-mailadres (mag vaker)")
    p.add_argument("--sinds", type=date.fromisoformat)
    p.add_argument("--uit", default=str(DATA / "voorbeelden.jsonl"))
    args = p.parse_args(argv)

    cfg = Config.uit_omgeving()
    eigen = [a.lower() for a in (args.adres or cfg.eigen_adressen)]
    if not eigen:
        print("Geef je eigen adres op met --adres of zet MAIL_ADRES in .env")
        return 2
    lev_domeinen = [d.strip() for d in os.environ.get("LEVERANCIERS_DOMEINEN", "").split(",") if d.strip()]
    verz = Verzamelaar(eigen, args.sinds, lev_domeinen)

    if args.imap:
        lees_imap(cfg, verz, args.sinds)
    elif args.pad:
        pad = Path(args.pad)
        if not pad.exists():
            print(f"Niet gevonden: {pad}")
            return 2
        for n, ruw in enumerate(berichten_uit_pad(pad), 1):
            try:
                verz.voeg_toe(ontleed(ruw))
            except Exception:
                verz.tellers["onleesbaar"] += 1
            if n % 1000 == 0:
                print(f"  {n} mails gelezen...")
    else:
        p.print_help()
        return 2

    paren, handtekeningen = verz.koppel()
    uit = Path(args.uit)
    uit.parent.mkdir(parents=True, exist_ok=True)
    with open(uit, "w", encoding="utf-8") as f:
        for paar in paren:
            f.write(json.dumps(paar, ensure_ascii=False) + "\n")
    soorten = Counter(p["soort"] for p in paren)
    print(f"Gelezen: {verz.tellers['gelezen']} mails, waarvan {verz.tellers['van jou']} van jou "
          f"en {verz.tellers['automatisch']} automatisch.")
    print(f"Voorbeelden: {len(paren)} ({', '.join(f'{k} {v}' for k, v in soorten.most_common())})")
    print(f"Geschreven naar {uit} ({uit.stat().st_size / 1_000_000:.1f} MB)")
    if handtekeningen:
        meest, aantal = handtekeningen.most_common(1)[0]
        regels = meest.splitlines()[1:]  # zonder de groetregel; die zet de agent er zelf in de juiste taal boven
        sugg = DATA / "handtekening_suggestie.txt"
        sugg.write_text("\n".join(r for r in regels if r.strip()) + "\n", encoding="utf-8")
        print(f"Meest gebruikte handtekening ({aantal}x) staat in {sugg}. Klopt hij, hernoem het bestand dan "
              f"naar handtekening.txt.")
    if verz.tellers.get("onleesbaar"):
        print(f"Let op: {verz.tellers['onleesbaar']} mails waren onleesbaar en zijn overgeslagen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
