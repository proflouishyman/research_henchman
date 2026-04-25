"""Keyed API adapters for orchestrator pulls."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from .base import PullAdapter
from .document_links import build_link_rows
from .io_utils import era_years_from_gap, write_json_records
from .seed_url_fetch import blocked_reason_hint, resolve_seed_rows
from contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


class KeyedApiAdapter(PullAdapter):
    """Base class for APIs that require env-provided credentials."""

    source_type = SourceType.KEYED_API
    env_key: str = ""
    env_aliases: List[str] = []
    # Optional OR-of-AND groups for credential shape support.
    # Example: [["API_KEY"], ["USERNAME", "PASSWORD"]]
    credential_sets: List[List[str]] = []

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.keyed_apis

    def validate(self, availability: SourceAvailability) -> str:
        if self.source_id not in availability.keyed_apis:
            missing = availability.missing_keys.get(self.source_id, self.env_key)
            return f"{self.source_id}: missing env key {missing}"
        return ""

    def credential_hint(self) -> str:
        """Return human-readable credential requirement string."""

        if self.credential_sets:
            groups = ["+".join(group) for group in self.credential_sets if group]
            return " OR ".join(groups)
        keys = [self.env_key, *self.env_aliases]
        keys = [key for key in keys if key]
        return " | ".join(keys) if keys else self.env_key

    def has_credentials(self) -> bool:
        """Check whether this adapter has any valid credential form."""

        if self.credential_sets:
            for group in self.credential_sets:
                if group and all(os.environ.get(key, "").strip() for key in group):
                    return True
            return False

        for key in [self.env_key, *self.env_aliases]:
            if key and os.environ.get(key, "").strip():
                return True
        return False

    @property
    def api_key(self) -> str:
        for key in [self.env_key, *self.env_aliases]:
            val = os.environ.get(key, "").strip()
            if val:
                return val
        return ""


class BlsAdapter(KeyedApiAdapter):
    """BLS public data API v2."""

    source_id = "bls"
    env_key = "BLS_API_KEY"
    env_aliases = ["BLS_REGISTRATION_KEY"]

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            era_start, era_end = era_years_from_gap(gap)
            # Fall back to a recent 5-year window when era bounds are unavailable.
            bls_start = str(era_start) if era_start is not None else "2019"
            bls_end = str(era_end) if era_end is not None else "2024"
            payload = {
                "seriesid": ["CUUR0000SA0"],
                "startyear": bls_start,
                "endyear": bls_end,
                "registrationkey": self.api_key,
            }
            req = urllib.request.Request(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rows: List[Dict[str, Any]] = []
            for series in body.get("Results", {}).get("series", []):
                for point in series.get("data", [])[:12]:
                    rows.append(point)
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            status = "completed" if rows else "partial"
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={"records": len(rows), "endpoint": "bls_timeseries"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class BeaAdapter(KeyedApiAdapter):
    """BEA API dataset metadata lookup."""

    source_id = "bea"
    env_key = "BEA_USER_ID"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            params = urllib.parse.urlencode(
                {
                    "UserID": self.api_key,
                    "method": "GETDATASETLIST",
                    "ResultFormat": "JSON",
                }
            )
            url = f"https://apps.bea.gov/api/data/?{params}"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rows = body.get("BEAAPI", {}).get("Results", {}).get("Dataset", [])
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            status = "completed" if rows else "partial"
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={"records": len(rows), "endpoint": "bea_dataset_list"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class CensusAdapter(KeyedApiAdapter):
    """Census API basic variable lookup endpoint."""

    source_id = "census"
    env_key = "CENSUS_API_KEY"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            params = urllib.parse.urlencode({"key": self.api_key})
            url = f"https://api.census.gov/data/timeseries/eits/mrts/variables.json?{params}"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            variables = body.get("variables", {}) if isinstance(body, dict) else {}
            rows = [{"name": k, **v} for k, v in list(variables.items())[:50] if isinstance(v, dict)]
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            status = "completed" if rows else "partial"
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={"records": len(rows), "endpoint": "census_mrts_variables"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class EbscoApiAdapter(KeyedApiAdapter):
    """EBSCO Discovery Service (EDS) API adapter.

    Auth flow:
      1. POST /authservice/rest/uidauth  → AuthToken (30-min TTL)
      2. GET  /edsapi/rest/createsession → SessionToken
      3. GET  /edsapi/rest/search        → records (DbId + AN)
      4. GET  /edsapi/rest/retrieve      → full-text HTML / PDF links
      5. GET  /edsapi/rest/endsession

    Falls back to seed click-through URLs when credentials are absent or invalid.
    """

    _AUTH_URL    = "https://eds-api.ebscohost.com/authservice/rest/uidauth"
    _SESSION_URL = "https://eds-api.ebscohost.com/edsapi/rest/createsession"
    _SEARCH_URL  = "https://eds-api.ebscohost.com/edsapi/rest/search"
    _RETRIEVE_URL = "https://eds-api.ebscohost.com/edsapi/rest/retrieve"
    _ENDSESSION_URL = "https://eds-api.ebscohost.com/edsapi/rest/endsession"

    source_id = "ebsco_api"
    env_key = "EBSCO_API_KEY"
    credential_sets = [
        ["EBSCO_API_KEY"],
        ["EBSCO_PROF", "EBSCO_PWD"],
        ["EBSCO_PROFILE_ID", "EBSCO_PROFILE_PASSWORD"],
    ]

    # ── EDS auth helpers ────────────────────────────────────────────────────

    def _get_auth_token(self, timeout: int) -> str:
        """Authenticate and return an AuthToken. Raises on failure."""
        user_id    = os.environ.get("EBSCO_PROF", "").strip()
        password   = os.environ.get("EBSCO_PWD", "").strip()
        profile_id = os.environ.get("EBSCO_PROFILE_ID", "").strip()
        if not user_id or not password:
            raise RuntimeError("EBSCO_PROF / EBSCO_PWD not set")
        body = json.dumps({
            "UserId": user_id,
            "Password": password,
            "InterfaceId": profile_id or "ehost",
        }).encode()
        req = urllib.request.Request(
            self._AUTH_URL, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="ignore")
            try:
                msg = json.loads(raw).get("Reason", raw[:120])
            except Exception:
                msg = raw[:120]
            raise RuntimeError(f"EDS auth {exc.code}: {msg}") from exc
        token = resp.get("AuthToken", "")
        if not token:
            raise RuntimeError(f"EDS auth returned no token: {resp}")
        return token

    def _create_session(self, auth_token: str, timeout: int) -> str:
        """Create an EDS session and return the SessionToken."""
        req = urllib.request.Request(
            f"{self._SESSION_URL}?guest=n",
            headers={
                "x-authenticationToken": auth_token,
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        token = resp.get("SessionToken", "")
        if not token:
            raise RuntimeError(f"EDS createsession returned no token: {resp}")
        return token

    def _end_session(self, auth_token: str, session_token: str) -> None:
        try:
            req = urllib.request.Request(
                self._ENDSESSION_URL,
                headers={
                    "x-authenticationToken": auth_token,
                    "x-sessionToken": session_token,
                },
                method="GET",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def _search(
        self, query: str, auth_token: str, session_token: str,
        db: str, timeout: int, era_start: Any, era_end: Any,
    ) -> List[Dict[str, Any]]:
        """Run EDS search; return list of raw record dicts."""
        params: Dict[str, str] = {
            "query":          f"AND,{urllib.parse.quote(query)}",
            "resultsperpage": "10",
            "pagenumber":     "1",
            "sort":           "relevance",
            "autosuggest":    "n",
        }
        if db:
            params["includefacets"] = "n"
        if era_start and era_end:
            # DT1/DT2 are YYYYMMDD date-range limiters for EDS
            params["limiter"] = f"DT1:{era_start}0101-{era_end}1231"
        url = f"{self._SEARCH_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "x-authenticationToken": auth_token,
                "x-sessionToken":        session_token,
                "Accept":                "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return (
            data.get("SearchResult", {})
                .get("Data", {})
                .get("Records", []) or []
        )

    def _retrieve(
        self, db_id: str, an: str,
        auth_token: str, session_token: str, timeout: int,
    ) -> Dict[str, Any]:
        """Retrieve full record by DbId + accession number."""
        params = {"dbid": db_id, "an": urllib.parse.quote(an, safe="")}
        url = f"{self._RETRIEVE_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "x-authenticationToken": auth_token,
                "x-sessionToken":        session_token,
                "Accept":                "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("Record", {})

    # ── record parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_items(record: Dict[str, Any]) -> Dict[str, str]:
        """Flatten Items list into {Name: Value} dict."""
        out: Dict[str, str] = {}
        for item in record.get("Items", []) or []:
            name = str(item.get("Name", "")).strip()
            val  = str(item.get("Data", item.get("Value", ""))).strip()
            if name and val:
                out[name] = val
        return out

    @staticmethod
    def _bib(record: Dict[str, Any]) -> Dict[str, Any]:
        return (
            record.get("RecordInfo", {})
                  .get("BibRecord", {}) or {}
        )

    def _parse_record(
        self, record: Dict[str, Any], gap_id: str, query: str,
        auth_token: str, session_token: str, timeout: int,
    ) -> Dict[str, Any]:
        """Convert one EDS record dict into a row for artifact storage."""
        header  = record.get("Header", {}) or {}
        db_id   = str(header.get("DbId", "")).strip()
        an      = str(header.get("An", "")).strip()
        bib     = self._bib(record)
        entity  = bib.get("BibEntity", {}) or {}
        items   = self._extract_items(record)

        # Title
        titles = entity.get("Titles", []) or []
        title  = titles[0].get("TitleFull", titles[0].get("Title", "")) if titles else ""

        # Authors
        rels   = bib.get("BibRelationships", {}) or {}
        contribs = rels.get("HasContributorRelationships", []) or []
        authors = []
        for c in contribs:
            person = (c.get("PersonEntity") or c.get("Relationship") or {})
            name   = (person.get("Name", {}) or {}).get("NameFull", "") or str(person.get("Name", ""))
            if name:
                authors.append(name)

        # Source/journal
        parts_of = rels.get("IsPartOfRelationships", []) or []
        journal  = ""
        pub_date = ""
        for p in parts_of:
            pe = p.get("BibEntity", {}) or {}
            jtitles = pe.get("Titles", []) or []
            if jtitles:
                journal = jtitles[0].get("TitleFull", jtitles[0].get("Title", ""))
            dates = pe.get("Dates", []) or []
            if dates:
                pub_date = str(dates[0].get("Y", ""))

        abstract = items.get("Abstract", "")
        doi = items.get("DOI", "")

        # Full-text links from inline record (search response may include some)
        ft = record.get("FullText", {}) or {}
        ft_avail = str(ft.get("Text", {}).get("Availability", "0")) == "1"
        ft_html  = ft.get("Text", {}).get("Value", "") if ft_avail else ""
        pdf_links = [
            lnk.get("Url", "")
            for lnk in (ft.get("Links", []) or [])
            if str(lnk.get("Type", "")).lower() in {"pdflink", "ebook-pdf"}
        ]

        # If we have DbId + AN and no full text yet, call retrieve for more
        if db_id and an and not ft_html and not pdf_links:
            try:
                full_rec = self._retrieve(db_id, an, auth_token, session_token, timeout)
                ft2      = full_rec.get("FullText", {}) or {}
                ft_avail2 = str(ft2.get("Text", {}).get("Availability", "0")) == "1"
                if ft_avail2:
                    ft_html = ft2.get("Text", {}).get("Value", "")
                pdf_links = [
                    lnk.get("Url", "")
                    for lnk in (ft2.get("Links", []) or [])
                    if str(lnk.get("Type", "")).lower() in {"pdflink", "ebook-pdf"}
                ]
                if not abstract:
                    items2 = self._extract_items(full_rec)
                    abstract = items2.get("Abstract", abstract)
            except Exception:
                pass

        # Derive quality label
        if ft_html or pdf_links:
            quality_label = "high"
            quality_rank  = 90
        elif abstract:
            quality_label = "medium"
            quality_rank  = 60
        else:
            quality_label = "seed"
            quality_rank  = 20

        # Canonical access URL
        access_url = ""
        if pdf_links:
            access_url = pdf_links[0]
        elif db_id and an:
            access_url = (
                f"https://search.ebscohost.com/login.aspx"
                f"?direct=true&db={db_id}&AN={urllib.parse.quote(an)}&site=eds-live"
            )

        return {
            "title":         title,
            "authors":       authors,
            "journal":       journal,
            "pub_date":      pub_date,
            "abstract":      abstract[:2000] if abstract else "",
            "doi":           doi,
            "db_id":         db_id,
            "accession_num": an,
            "url":           access_url,
            "pdf_url":       pdf_links[0] if pdf_links else "",
            "full_text_html": ft_html[:50000] if ft_html else "",
            "query":         query,
            "gap_id":        gap_id,
            "link_type":     "full_text" if (ft_html or pdf_links) else ("abstract" if abstract else "record"),
            "quality_label": quality_label,
            "quality_rank":  quality_rank,
            "source":        "eds_api",
        }

    # ── EIT (EBSCOhost Integration Toolkit) REST search ────────────────────
    # EIT requires: prof=<account_id>.<group>.eitws2 & pwd=<profile_password>
    # The account_id is institution-specific; set EBSCO_ACCOUNT_ID in .env.
    # (JHU library provided profile ID "eitws2" and password "ebs8451" but not
    # the account prefix — contact library IT to obtain the full qualified ID.)

    _EIT_URL = "http://eit.ebscohost.com/Services/SearchService.asmx/Search"

    def _eit_search(
        self, query: str, db: str, timeout: int,
        era_start: Any, era_end: Any,
    ) -> List[Dict[str, Any]]:
        """Call EIT REST API; return list of parsed record dicts."""
        account_id   = os.environ.get("EBSCO_ACCOUNT_ID", "").strip()
        profile_name = os.environ.get("EBSCO_PROFILE_ID", "eitws2").strip()
        pwd          = os.environ.get("EBSCO_PROFILE_PASSWORD", "").strip()
        group        = os.environ.get("EBSCO_GROUP_ID", "main").strip() or "main"

        if not account_id or not pwd:
            raise RuntimeError("EBSCO_ACCOUNT_ID / EBSCO_PROFILE_PASSWORD not set for EIT")

        full_profile = f"{account_id}.{group}.{profile_name}"
        params: Dict[str, str] = {
            "prof":     full_profile,
            "pwd":      pwd,
            "authType": "profile",
            "query":    query,
            "db":       db,
            "numrec":   "10",
            "format":   "detailed",
            "startrec": "1",
        }
        # EIT date limiting is done via query syntax — no standalone date param.
        # Append DT1/DT2 limiters the same way EBSCOhost search UI does.
        if era_start and era_end:
            params["query"] = f"{query} AND DT1:{era_start}0101-{era_end}1231"

        url = f"{self._EIT_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/xml"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="ignore")
            raise RuntimeError(f"EIT HTTP {exc.code}: {body[:200]}") from exc

        return self._parse_eit_xml(raw, query)

    @staticmethod
    def _parse_eit_xml(xml: str, query: str) -> List[Dict[str, Any]]:
        """Parse EIT XML response into standard row dicts."""
        import xml.etree.ElementTree as ET

        if "<Fault" in xml or "<fault" in xml.lower():
            import re
            msg = re.search(r"<Message>(.*?)</Message>", xml, re.DOTALL)
            raise RuntimeError(f"EIT fault: {msg.group(1)[:200] if msg else xml[:200]}")

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise RuntimeError(f"EIT XML parse error: {exc}") from exc

        # Strip namespaces for simpler XPath
        ns_strip = re.compile(r"\{[^}]+\}")

        def tag(el: ET.Element) -> str:
            return ns_strip.sub("", el.tag)

        def find_text(el: ET.Element, *tags: str) -> str:
            for t in tags:
                node = el.find(f".//{{{el.tag.split('}')[0][1:] if '{' in el.tag else ''}}}{t}")
                if node is None:
                    # Try without namespace
                    for child in el.iter():
                        if tag(child) == t and child.text:
                            return (child.text or "").strip()
                elif node.text:
                    return node.text.strip()
            return ""

        rows = []
        for record in root.iter():
            if tag(record) != "Record":
                continue
            title    = find_text(record, "atl")
            authors  = find_text(record, "aug")
            abstract = find_text(record, "ab")
            full_text = find_text(record, "abody")
            pub_info = find_text(record, "pubinfo")
            pdf_url  = find_text(record, "pdfLink")
            plink    = find_text(record, "plink")
            subjects = find_text(record, "su")
            rec_id   = find_text(record, "recordID")

            has_full    = bool(full_text or pdf_url)
            has_abstract = bool(abstract)
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")

            rows.append({
                "title":          title,
                "authors":        authors,
                "abstract":       (abstract or full_text)[:2000],
                "journal":        pub_info,
                "pdf_url":        pdf_url,
                "url":            plink or pdf_url,
                "subjects":       subjects,
                "record_id":      rec_id,
                "query":          query,
                "quality_label":  quality_label,
                "quality_rank":   90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":         "eit_api",
                "link_type":      "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })
        return rows

    # ── main pull ────────────────────────────────────────────────────────────

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        era_start, era_end = era_years_from_gap(gap)
        source_root = Path(run_dir) / gap.gap_id / self.source_id
        source_root.mkdir(parents=True, exist_ok=True)
        db = os.environ.get("EBSCO_DB", "bth").strip()

        api_rows: List[Dict[str, Any]] = []
        api_error = ""
        link_mode = ""

        # ── 1. Try EIT REST API (profile-based, stateless) ──────────────────
        try:
            api_rows  = self._eit_search(query, db, min(30, timeout_seconds), era_start, era_end)
            link_mode = "eit_api"
        except Exception as exc:
            eit_error = str(exc)[:200]
            api_error = f"EIT: {eit_error}"

        # ── 2. Fall back to EDS API (requires separate provisioning) ─────────
        if not api_rows:
            auth_token = session_token = ""
            try:
                auth_token    = self._get_auth_token(min(20, timeout_seconds))
                session_token = self._create_session(auth_token, min(15, timeout_seconds))
                per_rec       = max(10, timeout_seconds // 6)
                raw_records   = self._search(
                    query, auth_token, session_token,
                    db, min(30, timeout_seconds), era_start, era_end,
                )
                for rec in raw_records[:8]:
                    try:
                        api_rows.append(self._parse_record(
                            rec, gap.gap_id, query,
                            auth_token, session_token, per_rec,
                        ))
                    except Exception:
                        pass
                if api_rows:
                    link_mode = "eds_api"
            except Exception as exc:
                api_error = f"{api_error} | EDS: {str(exc)[:150]}"
            finally:
                if auth_token and session_token:
                    self._end_session(auth_token, session_token)

        # ── 3. Seed-link fallback ────────────────────────────────────────────
        if not api_rows:
            api_rows  = build_link_rows(
                self.source_id, query, gap.gap_id,
                limit_local=4, era_start=era_start, era_end=era_end,
            )
            link_mode = "provider_search_seed"

        root = write_json_records(api_rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(
            1 for r in api_rows
            if str(r.get("quality_label", "")).lower() in {"high", "medium"}
        )
        status = "completed" if pulled_docs > 0 else ("partial" if api_rows else "failed")

        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(api_rows),
            run_dir=root,
            artifact_type="json_records",
            status=status,
            stats={
                "records":     len(api_rows),
                "pulled_docs": pulled_docs,
                "seed_only":   pulled_docs <= 0,
                "api_error":   api_error,
                "link_mode":   link_mode,
            },
        )
