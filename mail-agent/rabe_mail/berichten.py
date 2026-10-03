"""E-mail ontleden, opschonen en een antwoord-concept opbouwen."""
from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

try:
    from zoneinfo import ZoneInfo
    NL_TIJD = ZoneInfo("Europe/Amsterdam")
except Exception:  # Windows zonder tzdata-pakket: dan de klok van de pc
    NL_TIJD = None

AFBEELDING_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_BIJLAGE_BYTES = 3_500_000
MAX_PDF_BYTES = 1_000_000
MAX_BIJLAGEN = 3

AUTOMATISCHE_AFZENDERS = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|bounce[s]?|notifications?|"
    r"notificaties?|automail|news(letter)?|nieuwsbrief|info-noreply)\b",
    re.I,
)

# Regels waarmee een geciteerd vorig bericht begint (NL, DE, EN, Outlook, webmail).
CITAAT_START = re.compile(
    r"^\s*(Op .{5,120}(schreef|heeft .{0,40}geschreven).{0,120}:\s*$"
    r"|Am .{5,120}schrieb.{0,120}:\s*$"
    r"|On .{5,120}wrote.{0,40}:\s*$"
    r"|-{2,}\s*(Oorspronkelijk bericht|Original Message|Ursprüngliche Nachricht|Doorgestuurd bericht|Forwarded message)\s*-{2,}"
    r"|(Van|From|Von)\s*:\s.+$)",
    re.I,
)


class _Tekst(HTMLParser):
    BLOK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.delen: list[str] = []
        self._negeer = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._negeer += 1
        if tag in self.BLOK:
            self.delen.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._negeer:
            self._negeer -= 1
        if tag in self.BLOK:
            self.delen.append("\n")

    def handle_data(self, data):
        if not self._negeer:
            self.delen.append(data)


def html_naar_tekst(inhoud: str) -> str:
    p = _Tekst()
    try:
        p.feed(inhoud)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html.unescape(inhoud))
    tekst = "".join(p.delen)
    tekst = re.sub(r"[ \t\xa0]+", " ", tekst)
    tekst = re.sub(r"\n\s*\n\s*\n+", "\n\n", tekst)
    return tekst.strip()


def zonder_citaat(tekst: str) -> str:
    """Knipt het geciteerde vorige bericht onder een antwoord weg."""
    regels = tekst.splitlines()
    uit: list[str] = []
    for regel in regels:
        if CITAAT_START.match(regel) and len("\n".join(uit).strip()) > 0:
            break
        if regel.lstrip().startswith(">"):
            continue
        uit.append(regel)
    return "\n".join(uit).strip()


def citaat_deel(tekst: str) -> str:
    """Het geciteerde deel van een bericht (wat na de citaatregel komt), zonder '>'-tekens."""
    regels = tekst.splitlines()
    for i, regel in enumerate(regels):
        if CITAAT_START.match(regel) and i > 0:
            rest = [re.sub(r"^\s*>\s?", "", r) for r in regels[i + 1:]]
            return "\n".join(rest).strip()
    quoted = [re.sub(r"^\s*>\s?", "", r) for r in regels if r.lstrip().startswith(">")]
    return "\n".join(quoted).strip()


@dataclass
class Bijlage:
    naam: str
    mime: str
    data: bytes

    def als_blok(self) -> dict | None:
        """Content-blok voor Claude (afbeelding of pdf), of None als het te groot/ander type is."""
        if self.mime in AFBEELDING_TYPES and len(self.data) <= MAX_BIJLAGE_BYTES:
            return {"type": "image", "source": {"type": "base64", "media_type": self.mime,
                                                "data": base64.standard_b64encode(self.data).decode()}}
        if self.mime == "application/pdf" and len(self.data) <= MAX_PDF_BYTES:
            return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                   "data": base64.standard_b64encode(self.data).decode()}}
        return None


@dataclass
class Mail:
    uid: str
    message_id: str
    onderwerp: str
    van_naam: str
    van_adres: str
    antwoord_adres: str
    antwoord_naam: str
    aan: list[str]
    datum: datetime | None
    tekst: str
    references: str
    kopregels: dict = field(default_factory=dict)
    bijlagen: list[Bijlage] = field(default_factory=list)
    vlaggen: set[str] = field(default_factory=set)

    @property
    def klant_adres(self) -> str:
        return (self.antwoord_adres or self.van_adres).lower()

    def kort(self, n: int = 6000) -> str:
        t = self.tekst.strip()
        return t if len(t) <= n else t[:n] + "\n[... ingekort ...]"


def _decodeer_deel(deel: Message) -> str:
    try:
        return deel.get_content()
    except Exception:
        payload = deel.get_payload(decode=True) or b""
        charset = deel.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def tekst_van(msg: EmailMessage) -> str:
    deel = None
    try:
        deel = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        deel = None
    if deel is None:
        if msg.is_multipart():
            return ""
        deel = msg
    inhoud = _decodeer_deel(deel)
    if deel.get_content_type() == "text/html":
        inhoud = html_naar_tekst(inhoud)
    return inhoud.replace("\r\n", "\n").strip()


def bijlagen_van(msg: EmailMessage) -> list[Bijlage]:
    uit = []
    for deel in msg.iter_attachments() if msg.is_multipart() else []:
        mime = deel.get_content_type()
        try:
            data = deel.get_payload(decode=True) or b""
        except Exception:
            continue
        naam = deel.get_filename() or mime.replace("/", ".")
        uit.append(Bijlage(naam=naam, mime=mime, data=data))
    # Inline afbeeldingen (bijv. foto's in de mailtekst) tellen ook mee.
    if msg.is_multipart():
        for deel in msg.walk():
            if deel.get_content_maintype() == "image" and deel.get("Content-Disposition", "").startswith("inline"):
                data = deel.get_payload(decode=True) or b""
                naam = deel.get_filename() or "inline-afbeelding"
                if not any(b.data == data for b in uit):
                    uit.append(Bijlage(naam=naam, mime=deel.get_content_type(), data=data))
    return uit


def ontleed(ruw: bytes, uid: str = "") -> Mail:
    msg = BytesParser(policy=policy.default).parsebytes(ruw)
    van_naam, van_adres = parseaddr(str(msg.get("From", "")))
    rt_naam, rt_adres = parseaddr(str(msg.get("Reply-To", "")))
    try:
        datum = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
        if datum and datum.tzinfo is None:
            datum = datum.replace(tzinfo=timezone.utc)
    except Exception:
        datum = None
    aan = [a for _, a in getaddresses([str(msg.get("To", "")), str(msg.get("Cc", ""))]) if a]
    kop = {k: str(msg.get(k, "")) for k in ("Auto-Submitted", "Precedence", "List-Unsubscribe", "List-Id",
                                            "X-Autoreply", "X-Autorespond", "In-Reply-To")}
    return Mail(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip(),
        onderwerp=str(msg.get("Subject", "")).strip(),
        van_naam=van_naam,
        van_adres=van_adres.lower(),
        antwoord_adres=rt_adres.lower() if rt_adres and rt_adres.lower() != van_adres.lower() else "",
        antwoord_naam=rt_naam,
        aan=[a.lower() for a in aan],
        datum=datum,
        tekst=tekst_van(msg),
        references=str(msg.get("References", "")).strip(),
        kopregels=kop,
        bijlagen=bijlagen_van(msg),
    )


def is_automatisch(mail: Mail) -> str:
    """Reden waarom dit een automatisch/bulk-bericht is, of '' als het van een mens lijkt te komen."""
    k = mail.kopregels
    if k.get("Auto-Submitted") and k["Auto-Submitted"].lower() != "no":
        return "Auto-Submitted"
    if k.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return "Precedence " + k["Precedence"]
    if k.get("X-Autoreply") or k.get("X-Autorespond"):
        return "autoreply"
    if k.get("List-Unsubscribe") or k.get("List-Id"):
        return "nieuwsbrief/lijst"
    lokaal = mail.van_adres.split("@")[0]
    if AUTOMATISCHE_AFZENDERS.match(lokaal):
        # Shopify-contactformulier komt van mailer@shopify.com met de klant in Reply-To: wel doorlaten.
        if mail.antwoord_adres and not AUTOMATISCHE_AFZENDERS.match(mail.antwoord_adres.split("@")[0]):
            return ""
        return "afzender " + lokaal
    return ""


AFSLUITING = {"nl": "Met vriendelijke groet,", "de": "Mit freundlichen Grüßen", "en": "Kind regards,"}


def citeer(mail: Mail, max_tekens: int = 4000) -> str:
    wie = formataddr((mail.antwoord_naam or mail.van_naam, mail.klant_adres)) if (mail.antwoord_naam or mail.van_naam) else mail.klant_adres
    wanneer = mail.datum.astimezone(NL_TIJD).strftime("%d-%m-%Y %H:%M") if mail.datum else "onbekende datum"
    tekst = mail.tekst.strip()
    if len(tekst) > max_tekens:
        tekst = tekst[:max_tekens] + "\n[...]"
    regels = "\n".join("> " + r for r in tekst.splitlines())
    return f"Op {wanneer} schreef {wie}:\n{regels}"


def bouw_antwoord(mail: Mail, *, van_adres: str, van_naam: str, tekst: str, handtekening: str,
                  taal: str = "nl", notitie: str = "") -> EmailMessage:
    """Een reply-concept met de juiste threading-kopregels, handtekening en geciteerd origineel."""
    msg = EmailMessage()
    msg["From"] = formataddr((van_naam, van_adres))
    naam = mail.antwoord_naam if mail.antwoord_adres else mail.van_naam
    msg["To"] = formataddr((naam, mail.klant_adres)) if naam else mail.klant_adres
    onderwerp = mail.onderwerp or "Uw bericht"
    if not re.match(r"^\s*(re|aw|antw|sv)\s*:", onderwerp, re.I):
        onderwerp = "Re: " + onderwerp
    msg["Subject"] = onderwerp
    if mail.message_id:
        msg["In-Reply-To"] = mail.message_id
        msg["References"] = (mail.references + " " + mail.message_id).strip()
    msg["Date"] = formatdate(localtime=True)
    domein = van_adres.split("@")[-1] if "@" in van_adres else None
    msg["Message-ID"] = make_msgid(domain=domein)
    afsluiting = AFSLUITING.get(taal, AFSLUITING["nl"])
    delen = []
    if notitie:
        delen.append(notitie)
    delen.append(tekst.strip())
    delen.append(f"{afsluiting}\n\n{handtekening.strip()}")
    delen.append(citeer(mail))
    msg.set_content("\n\n".join(delen) + "\n")
    return msg
