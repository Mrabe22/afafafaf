import json
import threading
from types import SimpleNamespace as NS

import pytest

from rabe_mail import berichten
from rabe_mail.config import Config
from rabe_mail.pijplijn import NOTITIE_KOP, Agent
from rabe_mail.stem import Voorbeeld, Voorbeelden
from tests.test_berichten import maak


def gebruik():
    return NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tekst_antwoord(data):
    return NS(stop_reason="end_turn", stop_details=None, usage=gebruik(),
              content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(data))])


def laatste_tekst(messages):
    inhoud = messages[-1]["content"]
    if isinstance(inhoud, str):
        return inhoud
    return "\n".join(b.get("text", "") for b in inhoud if isinstance(b, dict))


class NepClaude:
    """Speelt Claude na. Routeert op de opdrachttekst, zodat parallelle controles in elke volgorde werken."""

    def __init__(self, triage=None, stijl_eerst_afkeuren=True, concept_naar_rabe=False, weiger=False):
        self.triage = triage or {"categorie": "bestelling", "antwoord_nodig": True, "taal": "nl",
                                 "aanspreekvorm": "u", "klantnaam": "Jan", "ordernummers": ["RT104578"],
                                 "vragen": ["Waar is mijn pakket?"], "samenvatting": "Vraagt naar zending"}
        self.stijl_eerst_afkeuren = stijl_eerst_afkeuren
        self.concept_naar_rabe = concept_naar_rabe
        self.weiger = weiger
        self.aanroepen = []
        self.lock = threading.Lock()
        self.stijl_rondes = 0
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        with self.lock:
            self.aanroepen.append(kw)
        assert kw["fallbacks"] == "default"
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        tekst = laatste_tekst(kw["messages"])
        if kw.get("tools"):
            return self._schrijver(kw)
        if "Taak: sorteren" in tekst:
            return tekst_antwoord(self.triage)
        if "Controle 3 van 4" in tekst:
            with self.lock:
                self.stijl_rondes += 1
                afkeuren = self.stijl_eerst_afkeuren and self.stijl_rondes == 1
            if afkeuren:
                return tekst_antwoord({"akkoord": False, "problemen": [
                    {"fragment": "Ik hoop dat deze e-mail u goed bereikt.", "probleem": "robotzin",
                     "oplossing": "weghalen"}], "beslissing_rabe_nodig": False, "toelichting": ""})
        return tekst_antwoord({"akkoord": True, "problemen": [], "beslissing_rabe_nodig": False, "toelichting": ""})

    def _schrijver(self, kw):
        berichten_ = kw["messages"]
        laatste = berichten_[-1]["content"]
        if self.weiger:
            return NS(stop_reason="refusal", stop_details=NS(category="general_harms"), usage=gebruik(), content=[])
        if len(berichten_) == 1:
            return NS(stop_reason="tool_use", stop_details=None, usage=gebruik(), content=[
                NS(type="tool_use", id="t1", name="zoek_bestellingen", input={"ordernummer": "RT104578"})])
        if isinstance(laatste, list) and laatste and laatste[0].get("type") == "tool_result":
            return tekst_antwoord({"antwoord": "Beste Jan,\n\nIk hoop dat deze e-mail u goed bereikt. Uw pakket is "
                                               "onderweg met DPD.", "taal": "nl",
                                   "feiten": [{"bewering": "onderweg met DPD", "bron": "zoek_bestellingen"}],
                                   "naar_rabe": self.concept_naar_rabe,
                                   "reden_naar_rabe": "Klant wil korting" if self.concept_naar_rabe else ""})
        assert "controles keurden" in laatste
        return tekst_antwoord({"antwoord": "Beste Jan,\n\nUw pakket is onderweg met DPD.", "taal": "nl",
                               "feiten": [{"bewering": "onderweg met DPD", "bron": "zoek_bestellingen"}],
                               "naar_rabe": self.concept_naar_rabe,
                               "reden_naar_rabe": "Klant wil korting" if self.concept_naar_rabe else ""})


class NepShop:
    def __init__(self):
        self.gevraagd = []

    def bestellingen_van(self, email, ordernummer=""):
        self.gevraagd.append((email, ordernummer))
        return {"gevonden": True, "bestellingen": [{"ordernummer": "RT104578", "verzendstatus": "FULFILLED"}]}


def cfg():
    return Config(mail_adres="info@rabetrading.com", mail_wachtwoord="x", eigen_adressen=["info@rabetrading.com"],
                  leveranciers=["Leverancier BV"], max_herschrijfrondes=2)


def agent(claude, shop=None):
    vb = Voorbeelden([Voorbeeld(vraag="waar blijft mijn pakket bestelling", antwoord="Beste klant, hij is onderweg.",
                                onderwerp="pakket")])
    return Agent(cfg(), claude, shop or NepShop(), vb, stijlgids="Kort en vriendelijk.",
                 handtekening="Martijn Rabe\nRabe Trading")


def test_volledige_ronde_met_herschrijven():
    claude, shop = NepClaude(), NepShop()
    mail = berichten.ontleed(maak(onderwerp="Waar blijft RT104578?", tekst="Hoi, waar is mijn pakket?"), uid="1")
    uit = agent(claude, shop).verwerk(mail, lambda: [])
    assert uit.status == "klaar", uit.rapport
    assert shop.gevraagd == [("jan@example.nl", "RT104578")]  # altijd gescoped op de afzender
    assert len(uit.rapport["rondes"]) == 2  # stijl keurde ronde 1 af, ronde 2 akkoord
    body = uit.bericht.get_content()
    assert "Ik hoop dat" not in body and "onderweg met DPD" in body
    assert NOTITIE_KOP not in body
    assert "Martijn Rabe" in body
    # triage + schrijver (tool + concept) + 4 controles + herschrijven + 4 controles = 12 aanroepen
    assert len(claude.aanroepen) == 12
    assert uit.dollars > 0
    systeem = claude.aanroepen[0]["system"][0]["text"]
    assert "Leverancier BV" in systeem and "Kort en vriendelijk." in systeem
    schrijver = next(a for a in claude.aanroepen if a.get("tools"))
    assert "waar blijft mijn pakket" in laatste_tekst(schrijver["messages"][:1])  # voorbeeld meegegeven


def test_naar_rabe_krijgt_notitie_bovenaan():
    claude = NepClaude(stijl_eerst_afkeuren=False, concept_naar_rabe=True)
    mail = berichten.ontleed(maak(tekst="Ik wil korting"), uid="2")
    uit = agent(claude).verwerk(mail, [])
    assert uit.status == "naar_rabe"
    body = uit.bericht.get_content()
    assert body.startswith(NOTITIE_KOP) and "Klant wil korting" in body


def test_blijvend_afgekeurd_wordt_controleren():
    class AltijdAf(NepClaude):
        def create(self, **kw):
            if "Controle 1 van 4" in laatste_tekst(kw["messages"]) and not kw.get("tools"):
                with self.lock:
                    self.aanroepen.append(kw)
                return tekst_antwoord({"akkoord": False, "problemen": [
                    {"fragment": "onderweg met DPD", "probleem": "niet in de gegevens", "oplossing": "weg"}],
                    "beslissing_rabe_nodig": False, "toelichting": ""})
            return super().create(**kw)

    claude = AltijdAf(stijl_eerst_afkeuren=False)
    uit = agent(claude).verwerk(berichten.ontleed(maak(), uid="3"), [])
    assert uit.status == "controleren"
    assert len(uit.rapport["rondes"]) == 3  # 1 + 2 herschrijfrondes
    assert "feiten NIET akkoord" in uit.bericht.get_content()


def test_automatische_mail_kost_niets():
    claude = NepClaude()
    uit = agent(claude).verwerk(berichten.ontleed(maak(van="noreply@shop.nl")), [])
    assert uit.status == "overgeslagen" and not claude.aanroepen and uit.bericht is None


def test_nieuwsbrief_volgens_triage_geen_concept():
    claude = NepClaude(triage={"categorie": "nieuwsbrief_reclame", "antwoord_nodig": False, "taal": "nl",
                               "aanspreekvorm": "u", "klantnaam": "", "ordernummers": [], "vragen": [],
                               "samenvatting": "SEO-aanbod"})
    uit = agent(claude).verwerk(berichten.ontleed(maak()), [])
    assert uit.status == "overgeslagen" and len(claude.aanroepen) == 1


def test_weigering_geeft_geen_concept():
    uit = agent(NepClaude(weiger=True)).verwerk(berichten.ontleed(maak()), [])
    assert uit.status == "geweigerd" and uit.bericht is None


def test_tool_executor_onbekende_tool():
    a = agent(NepClaude())
    with pytest.raises(ValueError):
        a._voer_tool_uit("x@y.nl")("verwijder_alles", {})
