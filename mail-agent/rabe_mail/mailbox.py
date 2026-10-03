"""IMAP-toegang tot de Mijndomein-mailbox: nieuwe mails lezen (zonder ze als gelezen te markeren),
concepten in de map Concepten zetten en verwerkte mails een eigen label geven."""
from __future__ import annotations

import imaplib
import json
import re
import time
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from .berichten import Mail, ontleed

LABEL = "RabeAgent"
LABEL_FOUT = "RabeAgentFout"
MAANDEN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
CONCEPT_NAMEN = ["Drafts", "INBOX.Drafts", "Concepten", "INBOX.Concepten", "Concept", "INBOX/Drafts"]
VERZONDEN_NAMEN = ["Sent", "INBOX.Sent", "Verzonden", "INBOX.Verzonden", "Sent Items", "Verzonden items",
                   "Sent Messages", "INBOX/Sent"]
MAX_GESCHIEDENIS_BYTES = 3_000_000

_LIST_REGEL = re.compile(r'\((?P<vlaggen>[^)]*)\)\s+(?P<scheiding>"[^"]*"|NIL)\s+(?P<naam>.+)$')


def imap_datum(d: date) -> str:
    return f"{d.day:02d}-{MAANDEN[d.month - 1]}-{d.year}"


def quote(naam: str) -> str:
    return '"' + naam.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ontleed_list(regels: list[bytes]) -> list[tuple[set[str], str]]:
    mappen = []
    for r in regels:
        if not r:
            continue
        tekst = r.decode("utf-8", errors="replace") if isinstance(r, bytes) else str(r)
        m = _LIST_REGEL.match(tekst)
        if not m:
            continue
        naam = m.group("naam").strip()
        if naam.startswith('"') and naam.endswith('"'):
            naam = naam[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        vlaggen = {v.lower() for v in m.group("vlaggen").split()}
        mappen.append((vlaggen, naam))
    return mappen


class Mailbox:
    def __init__(self, host: str, poort: int, gebruiker: str, wachtwoord: str, staat_pad: Path | None = None):
        self.host, self.poort = host, poort
        self.gebruiker, self.wachtwoord = gebruiker, wachtwoord
        self.staat_pad = staat_pad
        self.imap: imaplib.IMAP4_SSL | None = None
        self.labels_mogelijk = False
        self._mappen: list[tuple[set[str], str]] | None = None
        self._staat: set[str] | None = None

    # -- verbinding -------------------------------------------------------------------------
    def __enter__(self):
        self.imap = imaplib.IMAP4_SSL(self.host, self.poort)
        self.imap.login(self.gebruiker, self.wachtwoord)
        self.selecteer("INBOX")
        return self

    def __exit__(self, *exc):
        try:
            if self.imap:
                self.imap.logout()
        except Exception:
            pass

    def selecteer(self, map_naam: str, alleen_lezen: bool = False) -> None:
        typ, _ = self.imap.select(quote(map_naam), readonly=alleen_lezen)
        if typ != "OK":
            raise RuntimeError(f"Map {map_naam!r} kan niet geopend worden")
        if map_naam == "INBOX" and not alleen_lezen:
            _, perm = self.imap.response("PERMANENTFLAGS")
            perm_tekst = b" ".join(p for p in perm if p).decode(errors="replace") if perm else ""
            self.labels_mogelijk = "\\*" in perm_tekst or LABEL.lower() in perm_tekst.lower()

    # -- mappen -----------------------------------------------------------------------------
    def mappen(self) -> list[tuple[set[str], str]]:
        if self._mappen is None:
            typ, data = self.imap.list()
            self._mappen = ontleed_list(data or []) if typ == "OK" else []
        return self._mappen

    def zoek_map(self, speciaal: str, namen: list[str]) -> str | None:
        mappen = self.mappen()
        for vlaggen, naam in mappen:
            if speciaal in vlaggen:
                return naam
        bestaand = {naam.lower(): naam for _, naam in mappen}
        for kandidaat in namen:
            if kandidaat.lower() in bestaand:
                return bestaand[kandidaat.lower()]
        return None

    def concepten_map(self) -> str:
        naam = self.zoek_map("\\drafts", CONCEPT_NAMEN)
        if not naam:
            raise RuntimeError("Geen map Concepten/Drafts gevonden in de mailbox")
        return naam

    def verzonden_map(self) -> str | None:
        return self.zoek_map("\\sent", VERZONDEN_NAMEN)

    # -- lokale staat (alleen als de server geen eigen labels toestaat) ---------------------
    def _laad_staat(self) -> set[str]:
        if self._staat is None:
            self._staat = set()
            if self.staat_pad and self.staat_pad.exists():
                self._staat = set(json.loads(self.staat_pad.read_text(encoding="utf-8")))
        return self._staat

    def _bewaar_staat(self) -> None:
        if self.staat_pad is not None and self._staat is not None:
            self.staat_pad.parent.mkdir(parents=True, exist_ok=True)
            self.staat_pad.write_text(json.dumps(sorted(self._staat), indent=0), encoding="utf-8")

    # -- lezen ------------------------------------------------------------------------------
    def nieuwe_uids(self, sinds: date) -> list[str]:
        criteria = ["SINCE", imap_datum(sinds), "UNANSWERED", "UNDELETED"]
        if self.labels_mogelijk:
            criteria += ["UNKEYWORD", LABEL]
        typ, data = self.imap.uid("SEARCH", *criteria)
        if typ != "OK":
            raise RuntimeError("Zoeken in INBOX mislukt")
        return [u.decode() for u in (data[0] or b"").split()]

    def haal(self, uid: str) -> Mail:
        typ, data = self.imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"Mail {uid} kon niet opgehaald worden")
        mail = ontleed(data[0][1], uid=uid)
        kop = data[0][0] + b" " + b" ".join(d for d in data[1:] if isinstance(d, bytes))
        mail.vlaggen = {v.decode() for v in imaplib.ParseFlags(kop)}
        return mail

    def al_verwerkt(self, mail: Mail) -> bool:
        if self.labels_mogelijk:
            return False  # de zoekopdracht sloot gelabelde mails al uit
        return (mail.message_id or mail.uid) in self._laad_staat()

    def markeer_verwerkt(self, mail: Mail) -> None:
        if self.labels_mogelijk:
            self.imap.uid("STORE", mail.uid, "+FLAGS", f"({LABEL})")
        else:
            self._laad_staat().add(mail.message_id or mail.uid)
            self._bewaar_staat()

    def noteer_fout(self, mail: Mail) -> bool:
        """Telt een mislukte poging. Geeft True als dit de tweede keer is (dan niet nog eens proberen)."""
        sleutel = "fout:" + (mail.message_id or mail.uid)
        if self.labels_mogelijk:
            if LABEL_FOUT in mail.vlaggen:
                return True
            self.imap.uid("STORE", mail.uid, "+FLAGS", f"({LABEL_FOUT})")
            return False
        staat = self._laad_staat()
        if sleutel in staat:
            return True
        staat.add(sleutel)
        self._bewaar_staat()
        return False

    def geschiedenis(self, adres: str, uitsluiten: str = "", aantal: int = 3) -> list[tuple[str, Mail]]:
        """De laatste mails van en aan dit adres (INBOX en Verzonden), oudste eerst."""
        resultaat: list[tuple[str, Mail]] = []
        adres_q = quote(adres)
        for map_naam, richting, veld in (("INBOX", "klant", "FROM"), (self.verzonden_map(), "rabe", "TO")):
            if not map_naam:
                continue
            try:
                self.selecteer(map_naam, alleen_lezen=True)
                typ, data = self.imap.uid("SEARCH", veld, adres_q)
                uids = [u.decode() for u in (data[0] or b"").split()] if typ == "OK" else []
                for uid in reversed(uids[-(aantal + 1):]):
                    typ, grootte = self.imap.uid("FETCH", uid, "(RFC822.SIZE)")
                    m = re.search(rb"RFC822\.SIZE (\d+)", grootte[0] if grootte and grootte[0] else b"")
                    if m and int(m.group(1)) > MAX_GESCHIEDENIS_BYTES:
                        continue
                    mail = self.haal(uid)
                    if uitsluiten and mail.message_id == uitsluiten:
                        continue
                    mail.bijlagen = []
                    resultaat.append((richting, mail))
                    if sum(1 for r, _ in resultaat if r == richting) >= aantal:
                        break
            except Exception:
                continue
            finally:
                self.selecteer("INBOX")
        resultaat.sort(key=lambda x: x[1].datum.timestamp() if x[1].datum else 0)
        return resultaat

    def verzonden_mails(self, sinds: date | None = None, maximaal: int = 5000):
        """Generator over mails in Verzonden (voor de voorbeelden-bouwer)."""
        naam = self.verzonden_map()
        if not naam:
            raise RuntimeError("Geen map Verzonden gevonden")
        yield from self._alle_mails(naam, sinds, maximaal)

    def inbox_mails(self, sinds: date | None = None, maximaal: int = 20000):
        yield from self._alle_mails("INBOX", sinds, maximaal)

    def _alle_mails(self, map_naam: str, sinds: date | None, maximaal: int):
        self.selecteer(map_naam, alleen_lezen=True)
        criteria = ["SINCE", imap_datum(sinds)] if sinds else ["ALL"]
        typ, data = self.imap.uid("SEARCH", *criteria)
        uids = [u.decode() for u in (data[0] or b"").split()] if typ == "OK" else []
        for uid in uids[-maximaal:]:
            try:
                yield self.haal(uid)
            except Exception:
                continue
        self.selecteer("INBOX")

    # -- schrijven --------------------------------------------------------------------------
    def bewaar_concept(self, bericht: EmailMessage) -> str:
        map_naam = self.concepten_map()
        typ, data = self.imap.append(quote(map_naam), "(\\Draft \\Seen)",
                                     imaplib.Time2Internaldate(time.time()), bericht.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"Concept opslaan in {map_naam!r} mislukt: {data!r}")
        return map_naam
