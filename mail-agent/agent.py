"""Rabe mail-agent. Leest nieuwe mails, schrijft conceptantwoorden en zet ze in de map Concepten.

Gebruik:
    python agent.py --test               verbinding met mailbox, Shopify en Claude testen (kost niets)
    python agent.py                      één ronde in de MODUS uit .env (standaard: droog)
    python agent.py --modus droog        niets in de mailbox aanraken; concepten + rapport naar de map uit/
    python agent.py --modus concept      concepten in de map Concepten van de mailbox zetten
    python agent.py --eml pad\naar\mail.eml   één opgeslagen mail droog verwerken (zonder mailbox)
Opties: --sinds JJJJ-MM-DD, --max N, --opnieuw (droog: ook al eerder droog verwerkte mails)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import anthropic

from rabe_mail import berichten
from rabe_mail.config import DATA, UIT, Config
from rabe_mail.mailbox import Mailbox
from rabe_mail.pijplijn import Agent, Uitkomst
from rabe_mail.shopify import Shopify
from rabe_mail.stem import STANDAARD_HANDTEKENING, Voorbeelden, laad_tekst

DROOG_STAAT = DATA / "droog_verwerkt.json"


def maak_shopify(cfg: Config) -> Shopify:
    return Shopify(cfg.shopify_shop, cfg.shopify_api_versie, token=cfg.shopify_token,
                   client_id=cfg.shopify_client_id, client_secret=cfg.shopify_client_secret,
                   winkel_url=cfg.winkel_url)


def test(cfg: Config) -> int:
    ok = True
    try:
        with Mailbox(cfg.imap_host, cfg.imap_poort, cfg.mail_adres, cfg.mail_wachtwoord) as mb:
            print(f"Mailbox: ingelogd op {cfg.imap_host} als {cfg.mail_adres}")
            print(f"  Concepten-map: {mb.concepten_map()}")
            print(f"  Verzonden-map: {mb.verzonden_map()}")
            print(f"  Eigen label op mails mogelijk: {'ja' if mb.labels_mogelijk else 'nee (lokaal bestand)'}")
    except Exception as e:
        ok = False
        print(f"Mailbox: MISLUKT ({e})")
    try:
        naam = maak_shopify(cfg).graphql("{ shop { name myshopifyDomain } }")["shop"]
        print(f"Shopify: verbonden met {naam['name']} ({naam['myshopifyDomain']})")
    except Exception as e:
        ok = False
        print(f"Shopify: MISLUKT ({e})")
    try:
        model = anthropic.Anthropic().models.retrieve(cfg.model)
        print(f"Claude: API-sleutel werkt, model {model.id} beschikbaar")
    except Exception as e:
        ok = False
        print(f"Claude: MISLUKT ({e})")
    v = Voorbeelden.laad(DATA / "voorbeelden.jsonl")
    print(f"Stem: {len(v)} voorbeelden, stijlgids {'aanwezig' if (DATA / 'stijlgids.md').exists() else 'ONTBREEKT'}, "
          f"handtekening {'aanwezig' if (DATA / 'handtekening.txt').exists() else 'standaard'}")
    return 0 if ok else 1


def schrijf_rapport(mail: berichten.Mail, uitkomst: Uitkomst) -> None:
    UIT.mkdir(exist_ok=True)
    stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
    basis = UIT / f"{stempel}_{mail.uid}_{uitkomst.status}"
    regels = [
        f"Status: {uitkomst.status}   Categorie: {uitkomst.categorie or '-'}   Kosten: ${uitkomst.dollars:.3f}",
        f"Reden: {uitkomst.reden or '-'}",
        f"Van: {mail.antwoord_naam or mail.van_naam} <{mail.klant_adres}>   Onderwerp: {mail.onderwerp}",
        "",
    ]
    if uitkomst.bericht is not None:
        regels += ["=" * 30 + " CONCEPT " + "=" * 30, uitkomst.bericht.get_content().strip(), ""]
    for i, ronde in enumerate(uitkomst.rapport.get("rondes", []), 1):
        regels.append(f"---- Controles ronde {i} ----")
        for k, u in ronde["controles"].items():
            regels.append(f"{k}: {'akkoord' if u['akkoord'] else 'NIET akkoord'}"
                          f"{' (beslissing Rabe nodig)' if u['beslissing_rabe_nodig'] else ''}")
            for p in u["problemen"]:
                regels.append(f"   - \"{p['fragment']}\": {p['probleem']} -> {p['oplossing']}")
    if uitkomst.rapport.get("tool_log"):
        regels.append("---- Opgezocht in Shopify ----")
        for t in uitkomst.rapport["tool_log"]:
            regels.append(f"{t['tool']}({json.dumps(t['invoer'], ensure_ascii=False)})"
                          f"{' FOUT' if t['fout'] else ''}")
    basis.with_suffix(".txt").write_text("\n".join(regels) + "\n", encoding="utf-8")
    basis.with_suffix(".json").write_text(json.dumps(uitkomst.rapport, ensure_ascii=False, indent=1,
                                                     default=str), encoding="utf-8")


def maak_agent(cfg: Config) -> Agent:
    return Agent(cfg, anthropic.Anthropic(), maak_shopify(cfg), Voorbeelden.laad(DATA / "voorbeelden.jsonl"),
                 laad_tekst(DATA / "stijlgids.md"), laad_tekst(DATA / "handtekening.txt", STANDAARD_HANDTEKENING))


def los_bestand(cfg: Config, pad: str) -> int:
    with open(pad, "rb") as f:
        mail = berichten.ontleed(f.read(), uid="eml")
    uitkomst = maak_agent(cfg).verwerk(mail, [])
    schrijf_rapport(mail, uitkomst)
    print(f"{uitkomst.categorie or '-'}: {uitkomst.status} ({uitkomst.reden or '-'}), ${uitkomst.dollars:.3f}")
    print(f"Rapport en concept staan in {UIT}")
    return 0


def ronde(cfg: Config, sinds: date, max_aantal: int, opnieuw: bool) -> int:
    agent = maak_agent(cfg)
    droog = cfg.modus == "droog"
    droog_staat = set(json.loads(DROOG_STAAT.read_text())) if droog and DROOG_STAAT.exists() else set()
    totaal, tellers = 0.0, {}
    with Mailbox(cfg.imap_host, cfg.imap_poort, cfg.mail_adres, cfg.mail_wachtwoord,
                 staat_pad=DATA / "verwerkt.json") as mb:
        if not droog and not mb.labels_mogelijk and os.environ.get("GITHUB_ACTIONS"):
            print("Deze mailbox staat geen eigen labels toe. In GitHub Actions kan de agent dan niet onthouden "
                  "welke mails al verwerkt zijn (dubbele concepten). Draai de agent op je pc.")
            return 1
        uids = mb.nieuwe_uids(sinds)
        print(f"{len(uids)} onbeantwoorde mail(s) sinds {sinds.isoformat()} ({cfg.modus}-modus)")
        verwerkt = 0
        for uid in uids:
            if verwerkt >= max_aantal:
                print(f"Maximum van {max_aantal} per ronde bereikt; de rest volgt in de volgende ronde.")
                break
            try:
                mail = mb.haal(uid)
            except Exception as e:
                print(f"[{uid}] ophalen mislukt: {e}")
                continue
            if mail.datum and mail.datum.date() < sinds:
                continue
            sleutel = mail.message_id or uid
            if mb.al_verwerkt(mail) or (droog and not opnieuw and sleutel in droog_staat):
                continue
            verwerkt += 1
            try:
                uitkomst = agent.verwerk(mail, lambda m=mail: mb.geschiedenis(m.klant_adres, m.message_id))
            except Exception as e:
                print(f"[{uid}] FOUT: {type(e).__name__}: {e}")
                if not droog and mb.noteer_fout(mail):
                    mb.markeer_verwerkt(mail)
                    print(f"[{uid}] tweede mislukte poging, overgeslagen. Beantwoord deze mail zelf.")
                continue
            totaal += uitkomst.dollars
            tellers[uitkomst.status] = tellers.get(uitkomst.status, 0) + 1
            print(f"[{uid}] {uitkomst.categorie or '-'}: {uitkomst.status}"
                  f"{' (' + uitkomst.reden + ')' if uitkomst.status in ('overgeslagen', 'geweigerd') else ''}"
                  f", ${uitkomst.dollars:.3f}")
            if droog:
                schrijf_rapport(mail, uitkomst)
                droog_staat.add(sleutel)
                DATA.mkdir(exist_ok=True)
                DROOG_STAAT.write_text(json.dumps(sorted(droog_staat)), encoding="utf-8")
                continue
            if uitkomst.bericht is not None:
                map_naam = mb.bewaar_concept(uitkomst.bericht)
                print(f"[{uid}] concept opgeslagen in {map_naam}")
            mb.markeer_verwerkt(mail)
    samenvatting = ", ".join(f"{k} {v}" for k, v in sorted(tellers.items())) or "niets te doen"
    print(f"Klaar: {samenvatting}. Kosten deze ronde: ${totaal:.2f}")
    if droog and tellers:
        print(f"Droog gedraaid: lees de concepten in {UIT}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rabe mail-agent")
    p.add_argument("--test", action="store_true", help="alleen verbindingen testen")
    p.add_argument("--modus", choices=["droog", "concept"])
    p.add_argument("--sinds", type=date.fromisoformat)
    p.add_argument("--max", type=int)
    p.add_argument("--opnieuw", action="store_true")
    p.add_argument("--eml", help="een opgeslagen .eml-bestand droog verwerken")
    args = p.parse_args(argv)

    cfg = Config.uit_omgeving()
    if args.modus:
        cfg.modus = args.modus
    if args.eml:
        cfg.modus = "droog"
    fouten = [f for f in cfg.controleer() if not (args.eml and f.startswith("MAIL_WACHTWOORD"))]
    if fouten:
        print("Instellingen niet compleet:\n  " + "\n  ".join(fouten))
        return 2
    if args.test:
        return test(cfg)
    if args.eml:
        return los_bestand(cfg, args.eml)

    vandaag = date.today()
    if cfg.modus == "concept":
        sinds = max(cfg.startdatum, args.sinds or vandaag - timedelta(days=14))
    else:
        sinds = args.sinds or cfg.startdatum or vandaag - timedelta(days=2)
        if cfg.startdatum:
            sinds = max(sinds, cfg.startdatum)
    return ronde(cfg, sinds, args.max or cfg.max_per_run, args.opnieuw)


if __name__ == "__main__":
    sys.exit(main())
