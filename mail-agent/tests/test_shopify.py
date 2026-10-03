from rabe_mail.shopify import Shopify, zoek_ordernummers


def order(naam, email):
    return {"name": naam, "email": email, "createdAt": "2026-09-30T10:00:00Z", "cancelledAt": None,
            "statusPageUrl": f"https://rabetrading.com/status/{naam}", "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "FULFILLED", "paymentGatewayNames": ["iDEAL"],
            "totalPriceSet": {"shopMoney": {"amount": "99.95", "currencyCode": "EUR"}},
            "shippingAddress": {"city": "Enter", "countryCodeV2": "NL"},
            "lineItems": {"nodes": [{"title": "Pomp X", "variantTitle": None, "quantity": 1, "sku": "P1",
                                     "product": {"handle": "pomp-x", "levertijd": {"value": "1-3 werkdagen"}}}]},
            "fulfillments": [{"createdAt": "2026-10-01T09:00:00Z", "status": "SUCCESS", "displayStatus": "IN_TRANSIT",
                              "estimatedDeliveryAt": None,
                              "trackingInfo": [{"company": "DPD", "number": "05123", "url": "https://dpd/x"}],
                              "events": {"nodes": [{"status": "IN_TRANSIT", "happenedAt": "2026-10-01T12:00:00Z"}]}}]}


class NepShopify(Shopify):
    def __init__(self, antwoorden):
        super().__init__("tr1d9f-jk", token="x")
        self.antwoorden = antwoorden
        self.queries = []

    def graphql(self, query, variabelen=None):
        self.queries.append(variabelen)
        return self.antwoorden.pop(0)


def test_bestellingen_alleen_van_afzender():
    s = NepShopify([{"orders": {"nodes": [order("RT104578", "jan@example.nl"), order("RT104678", "ander@x.nl")]}}])
    r = s.bestellingen_van("Jan@Example.nl")
    assert s.queries[0] == {"q": 'email:"jan@example.nl"'}
    assert r["gevonden"] and [b["ordernummer"] for b in r["bestellingen"]] == ["RT104578"]
    b = r["bestellingen"][0]
    assert b["zendingen"][0]["tracking"][0]["nummer"] == "05123"
    assert b["regels"][0]["levertijd_volgens_webshop"] == "1-3 werkdagen"
    assert "straat" not in str(b).lower()


def test_ordernummer_van_ander_wordt_niet_getoond():
    s = NepShopify([{"orders": {"nodes": [order("RT104578", "jan@example.nl")]}}])
    r = s.bestellingen_van("jan@example.nl", "RT104678")
    assert not r["gevonden"] and "Rabe" in r["melding"]
    s = NepShopify([{"orders": {"nodes": [order("RT104578", "jan@example.nl")]}}])
    assert s.bestellingen_van("jan@example.nl", "#RT 104578")["gevonden"]


def test_product_details_alleen_publieke_metafields():
    s = NepShopify([{"productByIdentifier": {
        "title": "Pomp X", "handle": "pomp-x", "onlineStoreUrl": None, "productType": "Pompen",
        "descriptionHtml": "<p>Sterke pomp</p>", "levertijd": {"value": "1-3 werkdagen"},
        "metafields": {"nodes": [{"key": "spec_debiet", "value": "60 l/min"}, {"key": "inkoopprijs", "value": "10"},
                                 {"key": "import_source", "value": "leverancier-x"}, {"key": "meegeleverd", "value": "slang"}]},
        "variants": {"nodes": [{"title": "Default", "sku": "P1", "barcode": "871", "price": "99.95",
                                "compareAtPrice": None, "availableForSale": True}]}}}])
    r = s.product_details("https://rabetrading.com/products/pomp-x?variant=1")
    assert s.queries[0] == {"h": "pomp-x"}
    spec = r["product"]["specificaties"]
    assert spec == {"spec_debiet": "60 l/min", "meegeleverd": "slang"}
    assert r["product"]["url"] == "https://rabetrading.com/products/pomp-x"
    assert "Sterke pomp" in r["product"]["beschrijving"]


def test_zoek_producten_sku_query():
    s = NepShopify([{"products": {"nodes": []}}])
    r = s.zoek_producten("400500")
    assert 'sku:"400500"' in s.queries[0]["q"] and not r["gevonden"]


def test_ordernummers_in_tekst():
    assert zoek_ordernummers("Mijn bestelling RT104578 en ook #1039, nogmaals rt 104578") == ["RT104578", "#1039"]
