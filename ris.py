"""Client for Austria's RIS Open Government Data API — standard library only.

Three things this wrapper exists to handle, all found by testing the live API:

1. **RIS searches but does not rank.** ``Suchworte=Aktiengesetz`` returns 1,423
   genuine hits *in alphabetical order*, so the Aktiengesetz itself is not on the
   first page — the top three are "2. Wohnrechtsänderungsgesetz" and two EU
   association agreements. Every search here is reranked locally by BM25.

2. **``Kurztitel`` is silently ignored.** Passing it returns 441,066 hits — the
   entire corpus — rather than an error. A caller who trusted it would think they
   had filtered when they had not. This client never sends it.

3. **The version tuple moved.** ``v2.5`` now 404s; ``v2.6`` is current.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

__version__ = "1.0.0"

API = "https://data.bka.gv.at/ris/api/v2.6"
UA = "arthurlegal-at-ris-mcp/%s (+https://github.com/beerbottle90/at-ris-mcp)" % __version__

# RIS pages in fixed sizes; the API rejects arbitrary integers.
PAGE_SIZES = {10: "Ten", 20: "Twenty", 50: "Fifty", 100: "OneHundred"}

# Applikation -> what it actually covers. Used to validate input and to explain
# the choice to the model.
LEGISLATION_APPS = {
    "BrKons": "Bundesrecht konsolidiert — consolidated federal law (default)",
    "BgblAuth": "Bundesgesetzblatt authentisch — the authentic gazette since 2004",
    "LrKons": "Landesrecht konsolidiert — consolidated provincial law",
}
CASELAW_APPS = {
    "Justiz": "OGH (Supreme Court) and the ordinary civil/criminal courts",
    "Vwgh": "Verwaltungsgerichtshof — Supreme Administrative Court",
    "Vfgh": "Verfassungsgerichtshof — Constitutional Court",
    "Bvwg": "Bundesverwaltungsgericht — Federal Administrative Court",
    "Lvwg": "Landesverwaltungsgerichte — provincial administrative courts",
}


class RisError(Exception):
    """An upstream failure worth explaining to the caller."""


def _get(url: str, timeout: int = 60) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RisError(
                "RIS returned 404 for %s. Note that API v2.5 was retired — this "
                "client uses v2.6." % url
            ) from exc
        raise RisError("HTTP %s from RIS: %s" % (exc.code, url)) from exc
    except urllib.error.URLError as exc:
        raise RisError("Could not reach RIS: %s" % exc.reason) from exc
    except ValueError as exc:
        raise RisError("RIS returned unparseable JSON: %s" % exc) from exc


def _listify(value: Any) -> List[Any]:
    """RIS collapses single-element arrays into a bare object. Undo that."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _items(value: Any) -> List[str]:
    """RIS wraps repeated values as ``{"item": x}`` or ``{"item": [x, y]}``."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("item")
    return [str(v) for v in _listify(value) if v not in (None, "")]


def _content_urls(data: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    ref = (data.get("Dokumentliste") or {}).get("ContentReference") or {}
    for r in _listify(ref):
        for url in _listify((r.get("Urls") or {}).get("ContentUrl")):
            if isinstance(url, dict) and url.get("DataType"):
                out[str(url["DataType"]).lower()] = url.get("Url", "")
    return out


class RisClient:
    def _search(self, app_group: str, application: str, params: Dict[str, Any],
                page_size: int, page: int) -> Dict[str, Any]:
        size = PAGE_SIZES.get(int(page_size))
        if size is None:
            raise RisError("page_size must be one of %s" % sorted(PAGE_SIZES))
        query = {"Applikation": application, "DokumenteProSeite": size,
                 "Seitennummer": max(1, int(page))}
        query.update({k: v for k, v in params.items() if v})
        url = "%s/%s?%s" % (API, app_group, urllib.parse.urlencode(query))
        payload = _get(url)
        results = (payload.get("OgdSearchResult") or {}).get("OgdDocumentResults") or {}
        hits = results.get("Hits") or {}
        try:
            total = int(hits.get("#text", 0))
        except (TypeError, ValueError):
            total = 0
        return {"total": total, "refs": _listify(results.get("OgdDocumentReference")),
                "request_url": url}

    # -- legislation ------------------------------------------------------ #
    def search_legislation(self, terms: str = "", title: str = "",
                           application: str = "BrKons", as_of: str = "",
                           page_size: int = 20, page: int = 1) -> Dict[str, Any]:
        if application not in LEGISLATION_APPS:
            raise RisError("application must be one of %s" % sorted(LEGISLATION_APPS))
        if not terms and not title:
            raise RisError("Provide `terms` (full text) or `title`.")
        params: Dict[str, Any] = {"Suchworte": terms, "Titel": title}
        if as_of:
            # Point-in-time: what the law looked like on this date.
            params["Fassung.FassungVom"] = as_of
        raw = self._search("Bundesrecht", application, params, page_size, page)
        return {**raw, "results": [self._norm_law(r) for r in raw["refs"]]}

    def _norm_law(self, ref: Dict[str, Any]) -> Dict[str, Any]:
        data = ref.get("Data") or {}
        meta = data.get("Metadaten") or {}
        tech = meta.get("Technisch") or {}
        gen = meta.get("Allgemein") or {}
        law = meta.get("Bundesrecht") or meta.get("Landesrecht") or {}
        sub = law.get("BrKons") or law.get("LrKons") or law.get("BgblAuth") or {}
        eli = law.get("Eli") or gen.get("DokumentUrl") or ""
        short = law.get("Kurztitel") or ""
        return {
            "id": tech.get("ID", ""),
            "title": short,
            # RIS embeds <br/> and the enacting history in the long title.
            "long_title": (law.get("Titel") or "").replace("<br/>", " · "),
            "eli": eli,
            "url": gen.get("DokumentUrl") or eli,
            "gazette": sub.get("Kundmachungsorgan", ""),
            "type": sub.get("Typ", ""),
            "changed": gen.get("Geaendert", ""),
            "formats": _content_urls(data),
            # Built from fields RIS returned, never invented.
            "citation": "%s%s (RIS %s)" % (
                short or "(untitled)",
                ", " + sub["Kundmachungsorgan"] if sub.get("Kundmachungsorgan") else "",
                tech.get("ID", ""),
            ),
        }

    # -- case law --------------------------------------------------------- #
    def search_caselaw(self, terms: str = "", application: str = "Justiz",
                       date_from: str = "", date_to: str = "",
                       page_size: int = 20, page: int = 1) -> Dict[str, Any]:
        if application not in CASELAW_APPS:
            raise RisError("application must be one of %s" % sorted(CASELAW_APPS))
        if not terms:
            raise RisError("`terms` is required for case-law search.")
        params: Dict[str, Any] = {"Suchworte": terms}
        if date_from:
            params["Entscheidungsdatum.Von"] = date_from
        if date_to:
            params["Entscheidungsdatum.Bis"] = date_to
        raw = self._search("Judikatur", application, params, page_size, page)
        return {**raw, "results": [self._norm_case(r, application) for r in raw["refs"]]}

    def _norm_case(self, ref: Dict[str, Any], application: str) -> Dict[str, Any]:
        data = ref.get("Data") or {}
        meta = data.get("Metadaten") or {}
        tech = meta.get("Technisch") or {}
        gen = meta.get("Allgemein") or {}
        jud = meta.get("Judikatur") or {}
        # Court-specific fields live in a sub-object named after the application;
        # the identifying fields sit directly on Judikatur.
        sub = jud.get(application) or {}
        if not sub:
            for key in ("Justiz", "Vwgh", "Vfgh", "Bvwg", "Lvwg"):
                if jud.get(key):
                    sub = jud[key]
                    break

        doc_type = jud.get("Dokumenttyp", "")
        court = sub.get("Gericht") or tech.get("Organ") or application
        docket = _items(jud.get("Geschaeftszahl"))
        norms = _items(jud.get("Normen"))
        decided = jud.get("Entscheidungsdatum", "")
        ecli = jud.get("EuropeanCaseLawIdentifier", "")

        out: Dict[str, Any] = {
            "id": tech.get("ID", ""),
            "court": court,
            "doc_type": doc_type,
            "docket": "; ".join(docket),
            "date": decided,
            "ecli": ecli,
            "norms": norms,
            "legal_areas": _items(sub.get("Rechtsgebiete")),
            "url": gen.get("DokumentUrl", ""),
            "formats": _content_urls(data),
        }

        if doc_type == "Rechtssatz":
            # A Rechtssatz is a legal proposition distilled from a line of cases,
            # not a judgment. Saying so matters: citing it as "the decision" would
            # misdescribe what it is, and it lists every case that applied it.
            out["rechtssatz_numbers"] = _items(sub.get("Rechtssatznummern"))
            decisions = []
            for d in _listify((sub.get("Entscheidungstexte") or {}).get("item")):
                if isinstance(d, dict):
                    decisions.append(
                        {
                            "docket": d.get("Geschaeftszahl", ""),
                            "court": d.get("Gericht", ""),
                            "date": d.get("Entscheidungsdatum", ""),
                            "url": d.get("DokumentUrl", ""),
                            "note": d.get("Anmerkung", ""),
                        }
                    )
            out["decisions"] = decisions
            out["note"] = (
                "Rechtssatz — a legal proposition abstracted from %d decision(s), "
                "not a single judgment. Cite the underlying decision from "
                "`decisions` when you need a judgment." % len(decisions)
            )
            rs_no = out["rechtssatz_numbers"]
            out["citation"] = " ".join(
                x for x in (court, rs_no[0] if rs_no else "", ecli) if x
            ) or "%s (RIS %s)" % (court, tech.get("ID", ""))
        else:
            out["citation"] = " ".join(
                x for x in (court, "; ".join(docket), decided, ecli) if x
            ) or "%s (RIS %s)" % (court, tech.get("ID", ""))
        return out

    # -- full text -------------------------------------------------------- #
    def fetch(self, url: str, max_chars: int = 60000) -> Dict[str, Any]:
        """Fetch one of the ``formats`` URLs a search result carried."""
        if not url.startswith(("https://ogd.ris.bka.gv.at/", "https://www.ris.bka.gv.at/")):
            # Refuse to be a generic fetcher: this server speaks for RIS only.
            raise RisError("Refusing to fetch a non-RIS URL: %s" % url)
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RisError("HTTP %s fetching %s" % (exc.code, url)) from exc
        except urllib.error.URLError as exc:
            raise RisError("Could not reach RIS: %s" % exc.reason) from exc
        out: Dict[str, Any] = {"url": url, "length_chars": len(raw), "text": raw[:max_chars]}
        if len(raw) > max_chars:
            out["truncated"] = "Truncated at %d of %d characters." % (max_chars, len(raw))
        return out
