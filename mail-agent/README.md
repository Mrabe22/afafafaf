# Rabe mail-agent

Deze agent leest nieuwe mails in je Mijndomein-mailbox, zoekt waar nodig gegevens op in Shopify (bestellingen, track & trace, producten, levertijd, adviesartikelen) en schrijft een conceptantwoord in jouw stijl. Dat concept staat daarna gewoon in de map Concepten van je webmail, als antwoord op de mail van de klant. Jij leest het, past het eventueel aan en drukt zelf op versturen. De agent verstuurt zelf nooit iets.

Elk concept gaat eerst door vier aparte controles: feiten (staat alles echt in Shopify of in de mail van de klant), jouw regels (geen toezeggingen over geld, geen leveranciersnamen, het niet-ontvangen-protocol), toon en stijl (kort, lopend verhaal, geen opsommingen, geen gedachtestreepjes) en volledigheid (is elke vraag beantwoord, moet jij iets beslissen). Keurt een controle iets af, dan herschrijft de agent het concept en controleert opnieuw, maximaal twee keer.

Komt een concept niet door alle controles, of moet jij iets beslissen (korting, retour, offerte, klacht), dan staat er bovenaan het concept een blok "NOTITIE VOOR RABE" met wat er aan de hand is. Dat blok haal je weg voordat je verstuurt. Concepten zonder notitie zijn door alle vier de controles gekomen.

## Wat de agent wel en niet doet

De agent markeert mails niet als gelezen, verstuurt niets en raakt mails van voor de STARTDATUM nooit aan. Mails die je al beantwoord hebt slaat hij over, net als nieuwsbrieven, automatische meldingen, bol.com- en Amazon-berichten en leveranciersmail. Bestellingen zoekt hij alleen op het e-mailadres van de afzender, dus iemand kan nooit gegevens van een andere klant loskrijgen door een ander ordernummer te noemen. Productinformatie komt alleen uit wat ook op de webshop staat: geen inkoop, marge of leverancier.

## Kosten

Je hebt een API-sleutel van Anthropic nodig (console.anthropic.com). Die staat los van je Claude-abonnement en wordt per gebruik afgerekend. Met het beste model (Claude Opus 5.5) kost een beantwoorde klantmail ongeveer $0,25 tot $0,60, afhankelijk van hoeveel er opgezocht en herschreven moet worden. Nieuwsbrieven en automatische mails kosten vrijwel niets, omdat die zonder AI herkend worden of alleen een korte sorteerstap krijgen. Elke ronde meldt wat hij gekost heeft. Wil je goedkoper, zet dan `MODEL_CONTROLES=claude-sonnet-5-5` in `.env`: de sorteerstap en de vier controles kosten dan ongeveer de helft.

## Installeren op je pc (stap voor stap)

1. Haal de code op en ga naar de map. In PowerShell:

   ```
   cd C:\Users\Barma
   git clone https://github.com/Mrabe22/afafafaf
   cd C:\Users\Barma\afafafaf
   git checkout claude/elegant-galileo-444nr9
   cd C:\Users\Barma\afafafaf\mail-agent
   python -m pip install -r requirements.txt
   ```

2. Kopieer `C:\Users\Barma\afafafaf\mail-agent\.env.voorbeeld` naar `C:\Users\Barma\afafafaf\mail-agent\.env` en vul in: je mailadres en wachtwoord van Mijndomein, je Anthropic API-sleutel, en de Shopify-gegevens (`SHOPIFY_CLIENT_ID` en `SHOPIFY_CLIENT_SECRET` staan in `C:\Users\Barma\shopify-mcp-server\.env.local`). Vul ook `LEVERANCIERS` en `LEVERANCIERS_DOMEINEN` in. Laat `MODUS=droog` staan.

3. Test de verbindingen. Dit kost niets:

   ```
   python agent.py --test
   ```

4. Maak van je mailexport een klein voorbeeldenbestand. Dit draait alleen op je pc en stuurt niets weg. Het pakt alle mails die jij verstuurd hebt, koppelt ze aan de vraag waarop je antwoordde en laat bijlagen weg:

   ```
   python tools\export_naar_voorbeelden.py "C:\pad\naar\je\mijndomein-export.zip"
   ```

   Heb je de export niet bij de hand, dan kan het ook rechtstreeks uit de mailbox: `python tools\export_naar_voorbeelden.py --imap --sinds 2024-01-01`. Het resultaat komt in `C:\Users\Barma\afafafaf\mail-agent\data\voorbeelden.jsonl`. Het script zet ook je meest gebruikte handtekening in `data\handtekening_suggestie.txt`. Klopt die, hernoem het bestand dan naar `handtekening.txt`.

5. Laat de stijlgids maken (eenmalig, ongeveer $0,20 tot $0,50):

   ```
   python tools\bouw_stijlgids.py
   ```

   Lees `C:\Users\Barma\afafafaf\mail-agent\data\stijlgids.md` daarna even na. Dit is hoe de agent jouw toon en jouw adviezen begrijpt; wat er niet klopt kun je gewoon aanpassen of weghalen.

6. Draai een proefronde. In droog-modus raakt de agent de mailbox niet aan en zet hij elk concept met het controlerapport in een tekstbestand:

   ```
   python agent.py --sinds 2026-09-28 --max 5
   ```

   De concepten staan in `C:\Users\Barma\afafafaf\mail-agent\uit\`. Je kunt ook één opgeslagen mail proberen: `python agent.py --eml "C:\pad\naar\mail.eml"`.

7. Bevalt het, zet dan in `.env` de `MODUS` op `concept` en `STARTDATUM` op de datum van vandaag. Vanaf dan komen de concepten in de map Concepten van je webmail.

8. Laat hem elke 10 minuten draaien met de Windows Taakplanner. In PowerShell:

   ```
   schtasks /Create /TN "Rabe mail-agent" /TR "C:\Users\Barma\afafafaf\mail-agent\deploy\start_agent.bat" /SC MINUTE /MO 10 /F
   ```

   Het logboek staat in `C:\Users\Barma\afafafaf\mail-agent\uit\agent.log`. Stoppen: `schtasks /Change /TN "Rabe mail-agent" /DISABLE`.

## Aanpassen

De regels waar de agent zich aan houdt staan in gewone tekstbestanden in `prompts\`: `regels.md` (jouw regels voor klantmails), `kennis.md` (vaste feiten zoals verzendkosten en telefoonnummer), `rol.md` en de vier controles. Verandert er iets, zoals je verzendkosten, pas dan `kennis.md` aan. Je stijl staat in `data\stijlgids.md`.

## In de cloud (optioneel)

Op je pc draait de agent alleen als de pc aan staat. Wil je hem ook laten draaien als de pc uit is, gebruik dan `deploy\mail-agent.yml` voor GitHub Actions. Doe dat alleen in een privé-repo, omdat de agent klantmail leest. De uitleg en de kosten in Actions-minuten staan bovenin dat bestand.
