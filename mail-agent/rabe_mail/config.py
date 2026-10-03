"""Instellingen uit omgevingsvariabelen, aangevuld met een lokaal .env-bestand (nooit in git)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
UIT = ROOT / "uit"

MODI = ("droog", "concept")

# Categorieen uit de triage waarop standaard een concept komt.
STANDAARD_CATEGORIEEN = ("klantvraag", "bestelling", "offerte", "retour_klacht")


def laad_env_bestand(pad: Path) -> None:
    """Zet KEY=waarde-regels uit pad in os.environ, zonder bestaande variabelen te overschrijven."""
    if not pad.exists():
        return
    for regel in pad.read_text(encoding="utf-8").splitlines():
        regel = regel.strip()
        if not regel or regel.startswith("#") or "=" not in regel:
            continue
        sleutel, waarde = regel.split("=", 1)
        waarde = waarde.strip()
        if len(waarde) >= 2 and waarde[0] == waarde[-1] and waarde[0] in "\"'":
            waarde = waarde[1:-1]
        os.environ.setdefault(sleutel.strip(), waarde)


def _lijst(waarde: str) -> list[str]:
    return [x.strip() for x in waarde.split(",") if x.strip()]


@dataclass
class Config:
    mail_adres: str
    mail_wachtwoord: str
    imap_host: str = "mail.mijndomein.nl"
    imap_poort: int = 993
    afzender_naam: str = "Rabe Trading"
    eigen_adressen: list[str] = field(default_factory=list)
    model: str = "claude-opus-5-5"
    model_controles: str = "claude-opus-5-5"
    modus: str = "droog"
    startdatum: date | None = None
    max_per_run: int = 10
    max_herschrijfrondes: int = 2
    categorieen: list[str] = field(default_factory=lambda: list(STANDAARD_CATEGORIEEN))
    leveranciers: list[str] = field(default_factory=list)
    shopify_shop: str = ""
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_token: str = ""
    shopify_api_versie: str = "2026-04"
    winkel_url: str = "https://rabetrading.com"

    @classmethod
    def uit_omgeving(cls) -> "Config":
        laad_env_bestand(ROOT / ".env")
        env = os.environ
        mail_adres = env.get("MAIL_ADRES", "").strip()
        start = env.get("STARTDATUM", "").strip()
        eigen = _lijst(env.get("EIGEN_ADRESSEN", ""))
        if mail_adres and mail_adres.lower() not in [a.lower() for a in eigen]:
            eigen.append(mail_adres)
        cfg = cls(
            mail_adres=mail_adres,
            mail_wachtwoord=env.get("MAIL_WACHTWOORD", ""),
            imap_host=env.get("IMAP_HOST", "mail.mijndomein.nl"),
            imap_poort=int(env.get("IMAP_POORT", "993")),
            afzender_naam=env.get("AFZENDER_NAAM", "Rabe Trading"),
            eigen_adressen=[a.lower() for a in eigen],
            model=env.get("MODEL", "claude-opus-5-5"),
            model_controles=env.get("MODEL_CONTROLES", env.get("MODEL", "claude-opus-5-5")),
            modus=env.get("MODUS", "droog").strip().lower(),
            startdatum=date.fromisoformat(start) if start else None,
            max_per_run=int(env.get("MAX_PER_RUN", "10")),
            max_herschrijfrondes=int(env.get("MAX_HERSCHRIJFRONDES", "2")),
            categorieen=_lijst(env.get("CATEGORIEEN", "")) or list(STANDAARD_CATEGORIEEN),
            leveranciers=_lijst(env.get("LEVERANCIERS", "")),
            shopify_shop=env.get("SHOPIFY_SHOP", "").strip(),
            shopify_client_id=env.get("SHOPIFY_CLIENT_ID", "").strip(),
            shopify_client_secret=env.get("SHOPIFY_CLIENT_SECRET", "").strip(),
            shopify_token=env.get("SHOPIFY_ADMIN_ACCESS_TOKEN", "").strip(),
            shopify_api_versie=env.get("SHOPIFY_API_VERSION", "2026-04"),
            winkel_url=env.get("WINKEL_URL", "https://rabetrading.com").rstrip("/"),
        )
        return cfg

    def controleer(self) -> list[str]:
        """Geeft een lijst met ontbrekende of foute instellingen (leeg = in orde)."""
        fouten = []
        if not self.mail_adres:
            fouten.append("MAIL_ADRES ontbreekt")
        if not self.mail_wachtwoord:
            fouten.append("MAIL_WACHTWOORD ontbreekt")
        if self.modus not in MODI:
            fouten.append(f"MODUS moet een van {MODI} zijn, niet {self.modus!r}")
        if self.modus == "concept" and not self.startdatum:
            fouten.append("STARTDATUM is verplicht in concept-modus (mails van voor die datum worden nooit aangeraakt)")
        if not (self.shopify_token or (self.shopify_client_id and self.shopify_client_secret)):
            fouten.append("Shopify-toegang ontbreekt (SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET, of SHOPIFY_ADMIN_ACCESS_TOKEN)")
        if not self.shopify_shop:
            fouten.append("SHOPIFY_SHOP ontbreekt (bijv. tr1d9f-jk.myshopify.com)")
        return fouten
