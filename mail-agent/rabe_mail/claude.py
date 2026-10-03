"""Aanroepen van Claude met JSON-uitvoer, optionele tools en kostenregistratie."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Prijzen in USD per miljoen tokens (invoer, uitvoer, cache lezen). Cache schrijven = 1,25 x invoer.
PRIJZEN = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}


class Geweigerd(RuntimeError):
    """Claude weigerde de aanvraag (ook na de automatische fallback)."""


@dataclass
class Verbruik:
    invoer: int = 0
    uitvoer: int = 0
    cache_lezen: int = 0
    cache_schrijven: int = 0
    dollars: float = 0.0

    def tel(self, model: str, usage) -> None:
        inv = getattr(usage, "input_tokens", 0) or 0
        uit = getattr(usage, "output_tokens", 0) or 0
        lees = getattr(usage, "cache_read_input_tokens", 0) or 0
        schrijf = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.invoer += inv
        self.uitvoer += uit
        self.cache_lezen += lees
        self.cache_schrijven += schrijf
        p_in, p_uit, p_lees = PRIJZEN.get(model, PRIJZEN["claude-opus-5-5"])
        self.dollars += (inv * p_in + uit * p_uit + lees * p_lees + schrijf * p_in * 1.25) / 1_000_000


@dataclass
class Gesprek:
    """Een JSON-gesprek met Claude dat je kunt voortzetten (alleen aanvullen, nooit herschrijven)."""
    client: anthropic.Anthropic
    model: str
    system: list[dict]
    schema: dict
    effort: str = "high"
    tools: list[dict] = field(default_factory=list)
    voer_tool_uit: Callable[[str, dict], str] | None = None
    verbruik: Verbruik = field(default_factory=Verbruik)
    berichten: list[dict] = field(default_factory=list)
    tool_log: list[dict] = field(default_factory=list)
    max_rondes: int = 8

    def vraag(self, inhoud) -> dict:
        self.berichten.append({"role": "user", "content": inhoud})
        for _ in range(self.max_rondes):
            kwargs = dict(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                messages=self.berichten,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": self.schema}},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            if self.tools:
                kwargs["tools"] = self.tools
            antwoord = self.client.beta.messages.create(**kwargs)
            self.verbruik.tel(self.model, antwoord.usage)
            if antwoord.stop_reason == "refusal":
                raise Geweigerd(getattr(antwoord.stop_details, "category", None) or "onbekend")
            if antwoord.stop_reason == "max_tokens":
                raise RuntimeError("Antwoord afgekapt op max_tokens")
            self.berichten.append({"role": "assistant", "content": antwoord.content})
            if antwoord.stop_reason == "pause_turn":
                continue
            if antwoord.stop_reason == "tool_use":
                resultaten = []
                for blok in antwoord.content:
                    if blok.type != "tool_use":
                        continue
                    try:
                        uitkomst = self.voer_tool_uit(blok.name, blok.input) if self.voer_tool_uit else "Geen tools."
                        fout = False
                    except Exception as e:  # tool-fout terug naar Claude, niet de run laten crashen
                        uitkomst, fout = f"Fout bij {blok.name}: {e}", True
                    self.tool_log.append({"tool": blok.name, "invoer": blok.input, "uitkomst": uitkomst,
                                          "fout": fout})
                    resultaten.append({"type": "tool_result", "tool_use_id": blok.id, "content": uitkomst,
                                       **({"is_error": True} if fout else {})})
                self.berichten.append({"role": "user", "content": resultaten})
                continue
            tekst = next((b.text for b in reversed(antwoord.content) if b.type == "text"), "")
            return json.loads(tekst)
        raise RuntimeError("Te veel tool-rondes zonder eindantwoord")


def json_vraag(client, model: str, system: list[dict], schema: dict, inhoud, effort: str,
               verbruik: Verbruik) -> dict:
    g = Gesprek(client=client, model=model, system=system, schema=schema, effort=effort, verbruik=verbruik)
    return g.vraag(inhoud)
