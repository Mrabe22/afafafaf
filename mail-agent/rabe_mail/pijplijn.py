"""Per mail: sorteren, concept schrijven (met Shopify-opzoekingen), vier controles, zo nodig herschrijven."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from typing import Callable

from . import berichten
from .berichten import Mail
from .claude import Geweigerd, Gesprek, Verbruik, json_vraag
from .config import PROMPTS, Config
from .shopify import Shopify, zoek_ordernummers
from .stem import Voorbeelden, formatteer_voorbeelden

TAAL = {"type": "string", "enum": ["nl", "de", "en"]}

SCHEMA_TRIAGE = {
    "type": "object",
    "properties": {
        "categorie": {"type": "string", "enum": [
            "klantvraag", "bestelling", "offerte", "retour_klacht", "leverancier", "marktplaats",
            "zakelijk_overig", "nieuwsbrief_reclame", "automatisch", "spam"]},
        "antwoord_nodig": {"type": "boolean"},
        "taal": {"type": "string", "enum": ["nl", "de", "en", "anders"]},
        "aanspreekvorm": {"type": "string", "enum": ["u", "je", "Sie", "du", "you"]},
        "klantnaam": {"type": "string"},
        "ordernummers": {"type": "array", "items": {"type": "string"}},
        "vragen": {"type": "array", "items": {"type": "string"}},
        "samenvatting": {"type": "string"},
    },
    "required": ["categorie", "antwoord_nodig", "taal", "aanspreekvorm", "klantnaam", "ordernummers",
                 "vragen", "samenvatting"],
    "additionalProperties": False,
}

SCHEMA_CONCEPT = {
    "type": "object",
    "properties": {
        "antwoord": {"type": "string"},
        "taal": TAAL,
        "feiten": {"type": "array", "items": {
            "type": "object",
            "properties": {"bewering": {"type": "string"}, "bron": {"type": "string"}},
            "required": ["bewering", "bron"], "additionalProperties": False}},
        "naar_rabe": {"type": "boolean"},
        "reden_naar_rabe": {"type": "string"},
    },
    "required": ["antwoord", "taal", "feiten", "naar_rabe", "reden_naar_rabe"],
    "additionalProperties": False,
}

SCHEMA_CONTROLE = {
    "type": "object",
    "properties": {
        "akkoord": {"type": "boolean"},
        "problemen": {"type": "array", "items": {
            "type": "object",
            "properties": {"fragment": {"type": "string"}, "probleem": {"type": "string"},
                           "oplossing": {"type": "string"}},
            "required": ["fragment", "probleem", "oplossing"], "additionalProperties": False}},
        "beslissing_rabe_nodig": {"type": "boolean"},
        "toelichting": {"type": "string"},
    },
    "required": ["akkoord", "problemen", "beslissing_rabe_nodig", "toelichting"],
    "additionalProperties": False,
}


def _tool(naam: str, beschrijving: str, veld: str, veld_uitleg: str) -> dict:
    return {
        "name": naam,
        "description": beschrijving,
        "strict": True,
        "input_schema": {"type": "object",
                         "properties": {veld: {"type": "string", "description": veld_uitleg}},
                         "required": [veld], "additionalProperties": False},
    }


TOOLS = [
    _tool("zoek_bestellingen",
          "Bestellingen van de afzender van deze mail: status, betaling, verzending, track & trace en de link naar "
          "de bestelstatuspagina. Zoekt alleen op het e-mailadres van de afzender.",
          "ordernummer", "Een ordernummer uit de mail (bijv. RT104578 of #1039), of een lege string voor de "
                         "recentste bestellingen van de afzender."),
    _tool("zoek_product",
          "Zoek actieve producten in de webshop op naam, type, artikelnummer (sku) of EAN. Geeft titel, handle, "
          "url, varianten met prijs incl. btw en de levertijd.",
          "zoekterm", "Zoekwoorden, artikelnummer of EAN."),
    _tool("product_details",
          "Alle publieke details van één product: beschrijving, specificaties, wat er meegeleverd wordt, "
          "levertijd en varianten.",
          "handle", "De handle uit zoek_product (of een productlink van rabetrading.com)."),
    _tool("zoek_advies",
          "Zoek in de adviesartikelen van Rabe op de webshop (keuzehulpen, uitleg, onderhoud).",
          "zoekterm", "Onderwerp in een paar woorden."),
]

CONTROLES = [
    ("feiten", "controle_1_feiten.md"),
    ("regels", "controle_2_regels.md"),
    ("stijl", "controle_3_stijl.md"),
    ("volledigheid", "controle_4_volledigheid.md"),
]

NOTITIE_KOP = "######## NOTITIE VOOR RABE: VERWIJDER DIT BLOK VOOR HET VERSTUREN ########"
NOTITIE_VOET = "#" * len(NOTITIE_KOP)


def prompt(naam: str) -> str:
    return (PROMPTS / naam).read_text(encoding="utf-8").strip()


@dataclass
class Uitkomst:
    status: str  # overgeslagen | klaar | naar_rabe | controleren | geweigerd
    reden: str = ""
    categorie: str = ""
    antwoord: str = ""
    taal: str = "nl"
    notitie: str = ""
    bericht: EmailMessage | None = None
    rapport: dict = field(default_factory=dict)
    dollars: float = 0.0

    @property
    def maakt_concept(self) -> bool:
        return self.bericht is not None


class Agent:
    def __init__(self, cfg: Config, client, shopify: Shopify, voorbeelden: Voorbeelden, stijlgids: str,
                 handtekening: str, vandaag: date | None = None):
        self.cfg = cfg
        self.client = client
        self.shopify = shopify
        self.voorbeelden = voorbeelden
        self.stijlgids = stijlgids
        self.handtekening = handtekening
        self.vandaag = vandaag or date.today()
        self.system = [{"type": "text", "text": self._systeemtekst(), "cache_control": {"type": "ephemeral"}}]

    def _systeemtekst(self) -> str:
        delen = [prompt("rol.md"), prompt("regels.md"), prompt("kennis.md")]
        if self.cfg.leveranciers:
            delen.append("# Namen die nooit in een klantmail mogen\n\n" + ", ".join(self.cfg.leveranciers) +
                         ". Dit zijn onze leveranciers. Noem ze nooit, ook niet als merk of afzender.")
        if self.stijlgids:
            delen.append("# Stijlgids van Rabe (afgeleid uit zijn eigen verzonden mails)\n\n" + self.stijlgids)
        else:
            delen.append("# Stijlgids van Rabe\n\nNog niet beschikbaar. Volg de regels hierboven en de voorbeelden.")
        return "\n\n".join(delen)

    # -- hulpfuncties ------------------------------------------------------------------------
    def _voer_tool_uit(self, klant_adres: str):
        def uitvoeren(naam: str, invoer: dict) -> str:
            if naam == "zoek_bestellingen":
                r = self.shopify.bestellingen_van(klant_adres, invoer.get("ordernummer", ""))
            elif naam == "zoek_product":
                r = self.shopify.zoek_producten(invoer.get("zoekterm", ""))
            elif naam == "product_details":
                r = self.shopify.product_details(invoer.get("handle", ""))
            elif naam == "zoek_advies":
                r = self.shopify.zoek_advies(invoer.get("zoekterm", ""))
            else:
                raise ValueError(f"Onbekende tool {naam}")
            return json.dumps(r, ensure_ascii=False)
        return uitvoeren

    @staticmethod
    def _geschiedenis_tekst(geschiedenis: list[tuple[str, Mail]]) -> str:
        if not geschiedenis:
            return "(geen eerdere mails met deze klant gevonden)"
        delen = []
        for richting, m in geschiedenis:
            wie = "klant" if richting == "klant" else "Rabe"
            wanneer = m.datum.strftime("%Y-%m-%d") if m.datum else "?"
            delen.append(f"<mail van=\"{wie}\" datum=\"{wanneer}\" onderwerp=\"{m.onderwerp}\">\n"
                         f"{berichten.zonder_citaat(m.tekst)[:1500]}\n</mail>")
        return "\n".join(delen)

    @staticmethod
    def _bijlagen(mail: Mail) -> tuple[list[dict], str]:
        blokken, regels = [], []
        for b in mail.bijlagen:
            blok = b.als_blok() if len(blokken) < berichten.MAX_BIJLAGEN else None
            if blok:
                blokken.append(blok)
                regels.append(f"{b.naam} ({b.mime}): hieronder bijgevoegd")
            else:
                regels.append(f"{b.naam} ({b.mime}): NIET gelezen (type of grootte)")
        return blokken, ("\n".join(regels) if regels else "(geen)")

    def _mail_blok(self, mail: Mail) -> str:
        wanneer = mail.datum.strftime("%Y-%m-%d %H:%M") if mail.datum else "?"
        naam = mail.antwoord_naam or mail.van_naam
        return (f"<klantmail van=\"{naam} <{mail.klant_adres}>\" datum=\"{wanneer}\" "
                f"onderwerp=\"{mail.onderwerp}\">\n{mail.kort()}\n</klantmail>")

    # -- stappen -----------------------------------------------------------------------------
    def triage(self, mail: Mail, verbruik: Verbruik) -> dict:
        inhoud = [{"type": "text", "text": self._mail_blok(mail)},
                  {"type": "text", "text": prompt("triage.md")}]
        return json_vraag(self.client, self.cfg.model_controles, self.system, SCHEMA_TRIAGE, inhoud, "low",
                          verbruik)

    def controleer(self, bundel: list[dict], verbruik: Verbruik) -> dict:
        """Vier controles. De eerste schrijft de gedeelde invoer in de cache, de andere drie lopen parallel."""
        def een(item):
            sleutel, bestand = item
            inhoud = bundel + [{"type": "text", "text": prompt(bestand)}]
            eigen = Verbruik()
            uitslag = json_vraag(self.client, self.cfg.model_controles, self.system, SCHEMA_CONTROLE, inhoud,
                                 "medium", eigen)
            return sleutel, uitslag, eigen

        uitslagen = [een(CONTROLES[0])]
        with ThreadPoolExecutor(max_workers=3) as pool:
            uitslagen += list(pool.map(een, CONTROLES[1:]))
        resultaat = {}
        for sleutel, uitslag, eigen in uitslagen:
            resultaat[sleutel] = uitslag
            for veld in ("invoer", "uitvoer", "cache_lezen", "cache_schrijven", "dollars"):
                setattr(verbruik, veld, getattr(verbruik, veld) + getattr(eigen, veld))
        return resultaat

    def _controle_bundel(self, mail: Mail, gesch_tekst: str, vb_tekst: str, bijlage_blokken: list[dict],
                         bijlage_tekst: str, gesprek: Gesprek, concept: dict) -> list[dict]:
        shopify_data = json.dumps(gesprek.tool_log, ensure_ascii=False, indent=1) if gesprek.tool_log else \
            "(de schrijver heeft niets in de webshop opgezocht)"
        tekst = (
            f"<datum_vandaag>{self.vandaag.isoformat()}</datum_vandaag>\n"
            f"{self._mail_blok(mail)}\n<bijlagen>\n{bijlage_tekst}\n</bijlagen>\n"
            f"<eerdere_mailwisseling>\n{gesch_tekst}\n</eerdere_mailwisseling>\n"
            f"<voorbeelden_van_rabe>\n{vb_tekst}\n</voorbeelden_van_rabe>\n"
            f"<shopify_gegevens>\n{shopify_data}\n</shopify_gegevens>\n"
            f"<concept taal=\"{concept['taal']}\">\n{concept['antwoord']}\n</concept>\n"
            f"<feiten_volgens_schrijver>\n{json.dumps(concept['feiten'], ensure_ascii=False)}\n"
            f"</feiten_volgens_schrijver>\n"
            f"<naar_rabe>{concept['naar_rabe']}: {concept['reden_naar_rabe']}</naar_rabe>")
        return bijlage_blokken + [{"type": "text", "text": tekst, "cache_control": {"type": "ephemeral"}}]

    # -- hoofdroute --------------------------------------------------------------------------
    def verwerk(self, mail: Mail,
                geschiedenis: Callable[[], list[tuple[str, Mail]]] | list[tuple[str, Mail]] | None = None
                ) -> Uitkomst:
        verbruik = Verbruik()
        if mail.van_adres in self.cfg.eigen_adressen and not mail.antwoord_adres:
            return Uitkomst("overgeslagen", "eigen mail")
        reden = berichten.is_automatisch(mail)
        if reden:
            return Uitkomst("overgeslagen", f"automatisch ({reden})")

        try:
            sortering = self.triage(mail, verbruik)
        except Geweigerd as e:
            return Uitkomst("geweigerd", f"sorteren geweigerd ({e})", dollars=verbruik.dollars)
        rapport = {"triage": sortering}
        categorie = sortering["categorie"]
        if not sortering["antwoord_nodig"] or categorie not in self.cfg.categorieen:
            return Uitkomst("overgeslagen", f"{categorie}, geen concept nodig", categorie=categorie,
                            rapport=rapport, dollars=verbruik.dollars)

        if callable(geschiedenis):
            geschiedenis = geschiedenis()
        geschiedenis = geschiedenis or []
        gesch_tekst = self._geschiedenis_tekst(geschiedenis)
        vb = self.voorbeelden.zoek(f"{mail.onderwerp}\n{berichten.zonder_citaat(mail.tekst)[:1500]}", k=3)
        vb_tekst = formatteer_voorbeelden(vb)
        bijlage_blokken, bijlage_tekst = self._bijlagen(mail)
        ordernummers = sorted(set(zoek_ordernummers(mail.onderwerp + "\n" + mail.tekst)) |
                              set(sortering["ordernummers"]))

        gesprek = Gesprek(client=self.client, model=self.cfg.model, system=self.system, schema=SCHEMA_CONCEPT,
                          effort="high", tools=TOOLS, voer_tool_uit=self._voer_tool_uit(mail.klant_adres),
                          verbruik=verbruik)
        opdracht = (
            f"<datum_vandaag>{self.vandaag.isoformat()}</datum_vandaag>\n"
            f"<sortering>{json.dumps(sortering, ensure_ascii=False)}</sortering>\n"
            f"<ordernummers_in_mail>{', '.join(ordernummers) or '(geen)'}</ordernummers_in_mail>\n"
            f"<eerdere_mailwisseling>\n{gesch_tekst}\n</eerdere_mailwisseling>\n"
            f"<voorbeelden_van_rabe>\n{vb_tekst}\n</voorbeelden_van_rabe>\n"
            f"{self._mail_blok(mail)}\n<bijlagen>\n{bijlage_tekst}\n</bijlagen>\n\n"
            f"{prompt('taak_concept.md')}")
        try:
            concept = gesprek.vraag(bijlage_blokken + [{"type": "text", "text": opdracht}])
            rondes = []
            for ronde in range(self.cfg.max_herschrijfrondes + 1):
                bundel = self._controle_bundel(mail, gesch_tekst, vb_tekst, bijlage_blokken, bijlage_tekst,
                                               gesprek, concept)
                uitslag = self.controleer(bundel, verbruik)
                rondes.append({"concept": concept, "controles": uitslag})
                problemen = [(k, p) for k, u in uitslag.items() if not u["akkoord"] for p in u["problemen"]]
                afgekeurd = [k for k, u in uitslag.items() if not u["akkoord"]]
                if not afgekeurd or ronde == self.cfg.max_herschrijfrondes:
                    break
                regels = "\n".join(f"- [{k}] \"{p['fragment']}\": {p['probleem']} Oplossing: {p['oplossing']}"
                                   for k, p in problemen) or "\n".join(
                    f"- [{k}] {uitslag[k]['toelichting']}" for k in afgekeurd)
                concept = gesprek.vraag(
                    "De controles keurden je concept nog niet goed. Pas het aan op deze punten en lever de "
                    "volledige verbeterde versie in hetzelfde formaat. Zoek in de webshop op wat je nog mist.\n\n"
                    + regels)
        except Geweigerd as e:
            rapport["tool_log"] = gesprek.tool_log
            return Uitkomst("geweigerd", f"Claude weigerde ({e}); zelf beantwoorden", categorie=categorie,
                            rapport=rapport, dollars=verbruik.dollars)

        laatste = rondes[-1]["controles"]
        afgekeurd = [k for k, u in laatste.items() if not u["akkoord"]]
        beslissing = concept["naar_rabe"] or any(u["beslissing_rabe_nodig"] for u in laatste.values())
        if afgekeurd:
            status = "controleren"
        elif beslissing:
            status = "naar_rabe"
        else:
            status = "klaar"
        rapport.update({"rondes": rondes, "tool_log": gesprek.tool_log,
                        "voorbeelden": [v.onderwerp for v in vb]})
        notitie = self._notitie(status, concept, laatste, len(rondes)) if status != "klaar" else ""
        bericht = berichten.bouw_antwoord(mail, van_adres=self.cfg.mail_adres, van_naam=self.cfg.afzender_naam,
                                          tekst=concept["antwoord"], handtekening=self.handtekening,
                                          taal=concept["taal"], notitie=notitie)
        return Uitkomst(status, concept["reden_naar_rabe"] if beslissing else "", categorie=categorie,
                        antwoord=concept["antwoord"], taal=concept["taal"], notitie=notitie, bericht=bericht,
                        rapport=rapport, dollars=verbruik.dollars)

    @staticmethod
    def _notitie(status: str, concept: dict, controles: dict, rondes: int) -> str:
        uitleg = {"naar_rabe": "naar Rabe: er is een beslissing of controle van jou nodig",
                  "controleren": f"controleren: na {rondes} ronde(s) nog niet door alle controles"}[status]
        regels = [NOTITIE_KOP, f"Status: {uitleg}"]
        if concept["naar_rabe"] and concept["reden_naar_rabe"]:
            regels.append(f"Waarom: {concept['reden_naar_rabe']}")
        stand = ", ".join(f"{k} {'akkoord' if u['akkoord'] else 'NIET akkoord'}" for k, u in controles.items())
        regels.append(f"Controles: {stand}")
        for k, u in controles.items():
            if u["beslissing_rabe_nodig"] and u["toelichting"]:
                regels.append(f"{k}: {u['toelichting']}")
            for p in u["problemen"] if not u["akkoord"] else []:
                regels.append(f"{k}: \"{p['fragment']}\" {p['probleem']}")
        regels.append(NOTITIE_VOET)
        return "\n".join(regels)
