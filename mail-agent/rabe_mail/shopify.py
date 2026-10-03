"""Alleen-lezen Shopify-opzoekingen voor de agent.

Veiligheid: bestellingen worden ALTIJD gezocht op het e-mailadres van de afzender van de mail. De agent kan
dus nooit gegevens van een andere klant opvragen, ook niet als iemand in een mail om een ander ordernummer
vraagt. Productinformatie bevat alleen wat ook op de webshop staat (geen inkoop, leverancier of marge)."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .berichten import html_naar_tekst

# Metafields die publiek op de productpagina staan en dus naar een klant mogen.
PUBLIEKE_METAFIELDS = {"levertijd", "meegeleverd", "verkoopeenheid", "garantie_tekst"}
PUBLIEKE_PREFIX = ("spec_",)


class ShopifyFout(RuntimeError):
    pass


class Shopify:
    def __init__(self, shop: str, api_versie: str = "2026-04", token: str = "", client_id: str = "",
                 client_secret: str = "", winkel_url: str = "https://rabetrading.com"):
        shop = shop.replace("https://", "").replace("http://", "").strip("/")
        if shop and "." not in shop:
            shop += ".myshopify.com"
        self.shop = shop
        self.api_versie = api_versie
        self.token = token
        self.client_id, self.client_secret = client_id, client_secret
        self.winkel_url = winkel_url.rstrip("/")

    # -- transport --------------------------------------------------------------------------
    def _haal_token(self) -> str:
        if self.token:
            return self.token
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        req = urllib.request.Request(f"https://{self.shop}/admin/oauth/access_token", data=form,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
        if not body.get("access_token"):
            raise ShopifyFout("Shopify gaf geen access token terug")
        self.token = body["access_token"]
        return self.token

    def graphql(self, query: str, variabelen: dict | None = None) -> dict:
        data = json.dumps({"query": query, "variables": variabelen or {}}).encode()
        for poging in range(3):
            req = urllib.request.Request(
                f"https://{self.shop}/admin/api/{self.api_versie}/graphql.json", data=data,
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self._haal_token()})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503) and poging < 2:
                    time.sleep(2 * (poging + 1))
                    continue
                raise ShopifyFout(f"Shopify HTTP {e.code}") from e
            if body.get("errors"):
                fouten = body["errors"]
                if any("THROTTLED" in json.dumps(f) for f in fouten) and poging < 2:
                    time.sleep(2 * (poging + 1))
                    continue
                raise ShopifyFout("Shopify GraphQL-fout: " + json.dumps(fouten)[:500])
            return body["data"]
        raise ShopifyFout("Shopify bleef afknijpen (throttled)")

    # -- bestellingen -----------------------------------------------------------------------
    ORDERS = """
    query($q: String!) {
      orders(first: 15, query: $q, sortKey: CREATED_AT, reverse: true) {
        nodes {
          name email createdAt cancelledAt statusPageUrl
          displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
          totalPriceSet { shopMoney { amount currencyCode } }
          shippingAddress { city countryCodeV2 }
          lineItems(first: 30) { nodes {
            title variantTitle quantity sku
            product { handle levertijd: metafield(namespace: "custom", key: "levertijd") { value } }
          } }
          fulfillments(first: 10) {
            createdAt status displayStatus estimatedDeliveryAt
            trackingInfo(first: 5) { company number url }
            events(first: 5, sortKey: HAPPENED_AT, reverse: true) { nodes { status happenedAt } }
          }
        }
      }
    }"""

    def bestellingen_van(self, email: str, ordernummer: str = "") -> dict:
        email = email.strip().lower()
        if not email or "@" not in email:
            return {"gevonden": False, "melding": "Geen geldig e-mailadres van de afzender."}
        data = self.graphql(self.ORDERS, {"q": f'email:"{email}"'})
        orders = [o for o in data["orders"]["nodes"] if (o.get("email") or "").lower() == email]
        if ordernummer:
            gezocht = _norm_order(ordernummer)
            orders = [o for o in orders if _norm_order(o["name"]) == gezocht]
            if not orders:
                return {"gevonden": False, "melding": (
                    f"Geen bestelling {ordernummer} gevonden bij het e-mailadres van de afzender. Mogelijk is "
                    "besteld met een ander e-mailadres; Rabe moet dit zelf nakijken. Noem de klant geen "
                    "gegevens die je niet hebt.")}
        if not orders:
            return {"gevonden": False, "melding": "Geen bestellingen gevonden bij het e-mailadres van de afzender."}
        return {"gevonden": True, "bestellingen": [self._order_samenvatting(o) for o in orders[:5]]}

    def _order_samenvatting(self, o: dict) -> dict:
        regels = []
        for li in o["lineItems"]["nodes"]:
            p = li.get("product") or {}
            regels.append({
                "product": li["title"] + (f" ({li['variantTitle']})" if li.get("variantTitle") else ""),
                "aantal": li["quantity"],
                "sku": li.get("sku"),
                "levertijd_volgens_webshop": (p.get("levertijd") or {}).get("value"),
                "productpagina": f"{self.winkel_url}/products/{p['handle']}" if p.get("handle") else None,
            })
        zendingen = []
        for f in o.get("fulfillments") or []:
            zendingen.append({
                "aangemaakt": f.get("createdAt"),
                "status": f.get("displayStatus") or f.get("status"),
                "verwachte_bezorging": f.get("estimatedDeliveryAt"),
                "tracking": [{"vervoerder": t.get("company"), "nummer": t.get("number"), "link": t.get("url")}
                             for t in f.get("trackingInfo") or []],
                "laatste_gebeurtenissen": [{"status": e["status"], "tijd": e["happenedAt"]}
                                           for e in (f.get("events") or {}).get("nodes", [])],
            })
        adres = o.get("shippingAddress") or {}
        return {
            "ordernummer": o["name"],
            "besteld_op": o["createdAt"],
            "geannuleerd_op": o.get("cancelledAt"),
            "betaalstatus": o.get("displayFinancialStatus"),
            "verzendstatus": o.get("displayFulfillmentStatus"),
            "betaalmethode": o.get("paymentGatewayNames"),
            "totaal": (o.get("totalPriceSet") or {}).get("shopMoney"),
            "bezorgplaats": ", ".join(x for x in [adres.get("city"), adres.get("countryCodeV2")] if x),
            "bestelstatuspagina": o.get("statusPageUrl"),
            "regels": regels,
            "zendingen": zendingen,
        }

    # -- producten --------------------------------------------------------------------------
    PRODUCTEN = """
    query($q: String!) {
      products(first: 6, query: $q) {
        nodes {
          title handle onlineStoreUrl productType
          levertijd: metafield(namespace: "custom", key: "levertijd") { value }
          variants(first: 10) { nodes { title sku barcode price compareAtPrice availableForSale } }
        }
      }
    }"""

    def zoek_producten(self, zoekterm: str) -> dict:
        term = re.sub(r'[\\"():]', " ", zoekterm).strip()
        if not term:
            return {"gevonden": False, "melding": "Lege zoekterm."}
        if re.fullmatch(r"[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*", term):
            q = f'status:active AND (sku:"{term}" OR barcode:"{term}" OR title:*{term}*)'
        else:
            q = f"status:active AND ({term})"
        data = self.graphql(self.PRODUCTEN, {"q": q})
        producten = [self._product_kort(p) for p in data["products"]["nodes"]]
        if not producten:
            return {"gevonden": False, "melding": f"Geen actieve producten gevonden voor '{zoekterm}'."}
        return {"gevonden": True, "producten": producten,
                "let_op": "Prijzen zijn webshopprijzen incl. btw. Gebruik product_details voor specificaties."}

    def _product_kort(self, p: dict) -> dict:
        return {
            "titel": p["title"],
            "handle": p["handle"],
            "url": p.get("onlineStoreUrl") or f"{self.winkel_url}/products/{p['handle']}",
            "type": p.get("productType"),
            "levertijd_volgens_webshop": (p.get("levertijd") or {}).get("value"),
            "varianten": [{"variant": v["title"], "sku": v.get("sku"), "ean": v.get("barcode"),
                           "prijs_incl_btw": v.get("price"), "van_prijs": v.get("compareAtPrice"),
                           "te_koop": v.get("availableForSale")} for v in p["variants"]["nodes"]],
        }

    PRODUCT = """
    query($h: String!) {
      productByIdentifier(identifier: {handle: $h}) {
        title handle onlineStoreUrl productType descriptionHtml
        levertijd: metafield(namespace: "custom", key: "levertijd") { value }
        metafields(first: 100, namespace: "custom") { nodes { key value } }
        variants(first: 20) { nodes { title sku barcode price compareAtPrice availableForSale } }
      }
    }"""

    def product_details(self, handle: str) -> dict:
        handle = handle.strip().split("/products/")[-1].split("?")[0].strip("/")
        data = self.graphql(self.PRODUCT, {"h": handle})
        p = data.get("productByIdentifier")
        if not p:
            return {"gevonden": False, "melding": f"Product '{handle}' niet gevonden."}
        uit = self._product_kort(p)
        beschrijving = html_naar_tekst(p.get("descriptionHtml") or "")
        uit["beschrijving"] = beschrijving[:4000]
        uit["specificaties"] = {m["key"]: m["value"] for m in p["metafields"]["nodes"]
                                if m["key"] in PUBLIEKE_METAFIELDS or m["key"].startswith(PUBLIEKE_PREFIX)}
        return {"gevonden": True, "product": uit}

    # -- adviesartikelen --------------------------------------------------------------------
    ARTIKELEN = """
    query($q: String!) {
      articles(first: 4, query: $q) {
        nodes { title handle isPublished body blog { handle } }
      }
    }"""

    def zoek_advies(self, zoekterm: str) -> dict:
        term = re.sub(r'[\\"():]', " ", zoekterm).strip()
        data = self.graphql(self.ARTIKELEN, {"q": term})
        artikelen = [{
            "titel": a["title"],
            "url": f"{self.winkel_url}/blogs/{a['blog']['handle']}/{a['handle']}",
            "tekst": html_naar_tekst(a.get("body") or "")[:2500],
        } for a in data["articles"]["nodes"] if a.get("isPublished")]
        if not artikelen:
            return {"gevonden": False, "melding": f"Geen adviesartikel gevonden voor '{zoekterm}'."}
        return {"gevonden": True, "artikelen": artikelen}


def _norm_order(naam: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", naam.upper())


def zoek_ordernummers(tekst: str) -> list[str]:
    """Ordernummers in een mailtekst: RT104578 of #1039."""
    gevonden = re.findall(r"\b(RT\s?\d{5,8})\b|#\s?(\d{4,6})\b", tekst, re.I)
    uit = []
    for rt, hekje in gevonden:
        n = rt.replace(" ", "").upper() if rt else f"#{hekje}"
        if n not in uit:
            uit.append(n)
    return uit
