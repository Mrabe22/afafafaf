from email.message import EmailMessage

from rabe_mail import berichten


def maak(van="Jan Jansen <jan@example.nl>", onderwerp="Vraag over pomp", tekst="Hallo,\n\nPast deze pomp?\n",
         **kop) -> bytes:
    m = EmailMessage()
    m["From"] = van
    m["To"] = "info@rabetrading.com"
    m["Subject"] = onderwerp
    m["Message-ID"] = "<abc123@example.nl>"
    m["Date"] = "Fri, 02 Oct 2026 10:00:00 +0200"
    for k, v in kop.items():
        m[k.replace("_", "-")] = v
    m.set_content(tekst)
    return m.as_bytes()


def test_ontleed_basis():
    mail = berichten.ontleed(maak(), uid="7")
    assert mail.van_adres == "jan@example.nl"
    assert mail.klant_adres == "jan@example.nl"
    assert mail.onderwerp == "Vraag over pomp"
    assert "Past deze pomp?" in mail.tekst
    assert mail.datum.year == 2026


def test_shopify_contactformulier_gebruikt_reply_to():
    ruw = maak(van="Rabe <mailer@shopify.com>", Reply_To="Piet <piet@boer.nl>")
    mail = berichten.ontleed(ruw)
    assert mail.klant_adres == "piet@boer.nl"
    assert berichten.is_automatisch(mail) == ""


def test_automatische_mails_herkend():
    assert berichten.is_automatisch(berichten.ontleed(maak(van="noreply@bol.com")))
    assert berichten.is_automatisch(berichten.ontleed(maak(List_Unsubscribe="<mailto:x@y.nl>")))
    assert berichten.is_automatisch(berichten.ontleed(maak(Auto_Submitted="auto-replied")))
    assert berichten.is_automatisch(berichten.ontleed(maak())) == ""


def test_zonder_citaat_nl_en_de():
    nl = "Bedankt, dat is duidelijk.\n\nOp 1 okt. 2026 om 10:00 schreef Rabe Trading <info@rabetrading.com>:\n> oud"
    assert berichten.zonder_citaat(nl) == "Bedankt, dat is duidelijk."
    de = "Danke schön.\n\nAm 01.10.2026 um 10:00 schrieb Rabe Trading:\n> alt"
    assert berichten.zonder_citaat(de) == "Danke schön."
    assert "oud" in berichten.citaat_deel(nl)


def test_html_naar_tekst():
    t = berichten.html_naar_tekst("<html><style>p{}</style><p>Hallo&nbsp;daar</p><p>Tweede</p></html>")
    assert "Hallo daar" in t and "Tweede" in t and "p{}" not in t


def test_bouw_antwoord_threading_en_handtekening():
    mail = berichten.ontleed(maak())
    bericht = berichten.bouw_antwoord(mail, van_adres="info@rabetrading.com", van_naam="Rabe Trading",
                                      tekst="Beste Jan,\n\nJa, die past.", handtekening="Martijn\nRabe Trading",
                                      taal="nl")
    assert bericht["Subject"] == "Re: Vraag over pomp"
    assert bericht["In-Reply-To"] == "<abc123@example.nl>"
    assert bericht["To"] == "Jan Jansen <jan@example.nl>"
    body = bericht.get_content()
    assert body.index("Ja, die past.") < body.index("Met vriendelijke groet,") < body.index("Martijn")
    assert "> Past deze pomp?" in body


def test_bouw_antwoord_duits_en_notitie_bovenaan():
    mail = berichten.ontleed(maak(onderwerp="AW: Anfrage"))
    bericht = berichten.bouw_antwoord(mail, van_adres="info@rabetrading.com", van_naam="Rabe Trading",
                                      tekst="Guten Tag,", handtekening="Rabe Trading", taal="de",
                                      notitie="#### NOTITIE ####")
    assert bericht["Subject"] == "AW: Anfrage"
    body = bericht.get_content()
    assert body.startswith("#### NOTITIE ####")
    assert "Mit freundlichen Grüßen" in body
