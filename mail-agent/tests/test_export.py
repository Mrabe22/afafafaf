import json
import mailbox
import zipfile
from email.message import EmailMessage

from rabe_mail.mailbox import imap_datum, ontleed_list, quote
from rabe_mail.stem import Voorbeelden
from tools import export_naar_voorbeelden as exp


def bericht(van, aan, onderwerp, tekst, mid, in_reply_to=None, datum="Thu, 01 Oct 2026 10:00:00 +0200"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = van, aan, onderwerp, mid, datum
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(tekst)
    return m


def test_export_zip_met_mbox_en_eml(tmp_path):
    vraag = bericht("Klant <klant@boer.nl>", "info@rabetrading.com", "Welke pomp voor 8 meter diep?",
                    "Hallo, mijn put is 8 meter diep. Bel me op 06 12345678.", "<v1@boer.nl>")
    antwoord = bericht("Rabe Trading <info@rabetrading.com>", "klant@boer.nl", "Re: Welke pomp voor 8 meter diep?",
                       "Beste klant,\n\nBij 8 meter diep raad ik een dompelpomp aan, een zuigpomp haalt dat niet.\n\n"
                       "Met vriendelijke groet,\n\nMartijn Rabe\nRabe Trading\n0547 231015\n\n"
                       "Op 1 okt. 2026 om 10:00 schreef Klant <klant@boer.nl>:\n> Hallo, mijn put is 8 meter diep.",
                       "<a1@rabetrading.com>", in_reply_to="<v1@boer.nl>")
    nieuwsbrief = bericht("news@leverancier.nl", "info@rabetrading.com", "Actie!", "Korting", "<n1@x.nl>")
    nieuwsbrief["List-Unsubscribe"] = "<mailto:uit@x.nl>"
    leverancier = bericht("Rabe <info@rabetrading.com>", "mark@groothandel.nl", "Re: offerte",
                          "Hoi Mark, kun je 50 stuks leveren naar Bremerhaven?\n\nGroet,\nMartijn",
                          "<a2@rabetrading.com>", in_reply_to="<l1@groothandel.nl>")
    lev_vraag = bericht("mark@groothandel.nl", "info@rabetrading.com", "offerte", "Wat heb je nodig?",
                        "<l1@groothandel.nl>")

    mbox_pad = tmp_path / "Sent.mbox"
    mb = mailbox.mbox(str(mbox_pad))
    mb.add(antwoord)
    mb.add(leverancier)
    mb.close()
    zip_pad = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_pad, "w") as z:
        z.write(mbox_pad, "export/Sent.mbox")
        z.writestr("export/INBOX/1.eml", vraag.as_bytes())
        z.writestr("export/INBOX/2.eml", nieuwsbrief.as_bytes())
        z.writestr("export/INBOX/3.eml", lev_vraag.as_bytes())
        z.writestr("export/leesmij.txt", "geen mail")

    verz = exp.Verzamelaar(["info@rabetrading.com"], None, ["groothandel.nl"])
    for ruw in exp.berichten_uit_pad(zip_pad):
        verz.voeg_toe(exp.ontleed(ruw))
    contactformulier = bericht("Rabe <info@rabetrading.com>", "info@rabetrading.com", "Nieuw klantbericht",
                               "Hebben jullie ook slangen van 50 meter?", "<c1@shopify.com>")
    contactformulier["Reply-To"] = "Kees <kees@x.nl>"
    verz.voeg_toe(exp.ontleed(contactformulier.as_bytes()))
    assert verz.tellers["van jou"] == 2 and verz.tellers["automatisch"] == 1
    assert "<c1@shopify.com>" in verz.inkomend
    paren, handtekeningen = verz.koppel()
    soorten = {p["soort"]: p for p in paren}
    klant = soorten["klant"]
    assert "8 meter diep" in klant["vraag"] and "[telefoon]" in klant["vraag"]
    assert klant["antwoord"].startswith("Beste klant") and "dompelpomp" in klant["antwoord"]
    assert "Met vriendelijke groet" not in klant["antwoord"] and "> Hallo" not in klant["antwoord"]
    assert klant["taal"] == "nl"
    assert soorten["leverancier"]["vraag"] == "Wat heb je nodig?"
    assert handtekeningen.most_common(1)[0][0].startswith("Met vriendelijke groet")

    uit = tmp_path / "voorbeelden.jsonl"
    uit.write_text("\n".join(json.dumps(p) for p in paren), encoding="utf-8")
    index = Voorbeelden.laad(uit)
    assert len(index) == 1  # alleen klantmails
    assert index.zoek("Hoe diep kan een pomp zuigen? Mijn put is 9 meter")[0].antwoord == klant["antwoord"]


def test_maskeren_laat_eigen_gegevens_staan():
    t = exp.maskeer("Bel 0547 231015 of 06-12345678, mail info@rabetrading.com of jan@x.nl, "
                    "IBAN NL67 RABO 0387 1228 42 of NL91ABNA0417164300", ["info@rabetrading.com"])
    assert "0547 231015" in t and "[telefoon]" in t
    assert "info@rabetrading.com" in t and "[e-mail]" in t
    assert "NL67 RABO 0387 1228 42" in t and "[iban]" in t


def test_imap_hulpjes():
    mappen = ontleed_list([b'(\\HasNoChildren \\Drafts) "." "INBOX.Drafts"', b'(\\HasNoChildren) "/" Sent',
                           b'(\\HasNoChildren \\Sent) "." "Verzonden items"'])
    assert mappen[0] == ({"\\hasnochildren", "\\drafts"}, "INBOX.Drafts")
    assert mappen[2][1] == "Verzonden items"
    assert quote('Map "x"') == '"Map \\"x\\""'
    from datetime import date
    assert imap_datum(date(2026, 10, 3)) == "03-Oct-2026"
