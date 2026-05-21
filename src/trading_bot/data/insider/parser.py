"""Parse Form 4 `primary_doc.xml` into `Form4Filing` rows.

Form 4 XML structure (relevant elements):

  <ownershipDocument>
    <periodOfReport>YYYY-MM-DD</periodOfReport>
    <issuer>
      <issuerCik>0000123456</issuerCik>
      <issuerName>...</issuerName>
      <issuerTradingSymbol>...</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
      <reportingOwnerId>
        <rptOwnerCik>...</rptOwnerCik>
        <rptOwnerName>...</rptOwnerName>
      </reportingOwnerId>
      <reportingOwnerRelationship>
        <isDirector>1</isDirector>
        <isOfficer>1</isOfficer>
        <isTenPercentOwner>0</isTenPercentOwner>
        <officerTitle>Chief Executive Officer</officerTitle>
      </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
      <nonDerivativeTransaction>
        <transactionDate><value>YYYY-MM-DD</value></transactionDate>
        <transactionCoding>
          <transactionCode>P</transactionCode>
        </transactionCoding>
        <transactionAmounts>
          <transactionShares><value>1000</value></transactionShares>
          <transactionPricePerShare><value>12.34</value></transactionPricePerShare>
          <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
        </transactionAmounts>
        <postTransactionAmounts>
          <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
        </postTransactionAmounts>
      </nonDerivativeTransaction>
    </nonDerivativeTable>
    <footnotes>...</footnotes>
  </ownershipDocument>

We only emit non-derivative transactions. Derivative transactions (option
grants, exercises) are explicitly out of scope per the schema contract.

Booleans are EDGAR's "1"/"0" or "true"/"false"; we coerce both.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from .schema import Form4Filing

logger = logging.getLogger(__name__)

_TRUE_TOKENS = frozenset({"1", "true", "True", "TRUE"})

# Detects mentions of Rule 10b5-1 in a footnote or explanation field.
# Patterns observed in filings include "10b5-1", "10b5‑1" (Unicode hyphen),
# and "Rule 10b5-1". Match case-insensitively across both.
_RULE_10B5_1 = re.compile(r"10b5[\-‐-―]?1", re.IGNORECASE)


class Form4ParseError(ValueError):
    """Raised when a Form 4 XML is malformed or missing required fields."""


def _text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    txt = elem.text
    if txt is None:
        return None
    txt = txt.strip()
    return txt or None


def _value_text(parent: ET.Element | None, tag: str) -> str | None:
    """Form 4 wraps many leaf fields as <tag><value>...</value></tag>.

    Some filings inline the value directly under <tag>; handle both.
    """
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None:
        return None
    value = child.find("value")
    if value is not None:
        return _text(value)
    return _text(child)


def _bool_value(parent: ET.Element | None, tag: str) -> bool:
    raw = _value_text(parent, tag)
    if raw is None:
        return False
    return raw in _TRUE_TOKENS


def _parse_date(raw: str | None, *, context: str) -> date:
    if not raw:
        raise Form4ParseError(f"missing date for {context}")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise Form4ParseError(f"bad date {raw!r} for {context}") from exc


def _parse_decimal(raw: str | None, *, default: Decimal = Decimal("0")) -> Decimal:
    if raw is None or raw == "":
        return default
    try:
        # Some filings include commas in numeric values.
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return default


def _pad_cik(raw: str | None) -> str:
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.zfill(10) if digits else ""


def _collect_footnote_text(root: ET.Element) -> str:
    """Concatenate all footnote text. Used for the 10b5-1 marker scan.

    EDGAR forms vary on where insiders disclose 10b5-1 plans — sometimes
    in <footnote>, sometimes free-text in <remarks> or <explanationOfResponse>.
    We scan all of them.
    """
    parts: list[str] = []
    for tag in ("footnote", "remarks", "explanationOfResponse", "footnoteId"):
        for elem in root.iter(tag):
            txt = _text(elem)
            if txt:
                parts.append(txt)
    return " ".join(parts)


def parse_form4_xml(
    xml_bytes: bytes,
    *,
    accession_no: str,
    filing_date: date,
    ticker_override: str | None = None,
) -> list[Form4Filing]:
    """Parse one `primary_doc.xml` into zero or more Form4Filing rows.

    Parameters
    ----------
    xml_bytes : raw XML body
    accession_no : SEC accession number for the filing (e.g. "0001234567-26-000001")
    filing_date : date EDGAR accepted the filing (already UTC)
    ticker_override : if provided, used directly; otherwise we fall back to
        the issuerTradingSymbol element. Resolution via CIK->ticker happens
        outside the parser (see `tickers.TickerMap`).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise Form4ParseError(f"invalid XML for {accession_no}: {exc}") from exc

    issuer = root.find("issuer")
    if issuer is None:
        raise Form4ParseError(f"{accession_no}: missing <issuer>")
    issuer_cik = _pad_cik(_text(issuer.find("issuerCik")))
    issuer_name = _text(issuer.find("issuerName")) or ""
    xml_ticker = _text(issuer.find("issuerTradingSymbol"))
    ticker = ticker_override or (xml_ticker.upper() if xml_ticker else None)

    # First reportingOwner only. The vast majority of Form 4s have a single
    # owner; the few group filings list multiple owners but a single set of
    # transactions, so attributing transactions across multiple owners is
    # ambiguous. Surface that as a parser limitation rather than guess.
    owner = root.find("reportingOwner")
    if owner is None:
        raise Form4ParseError(f"{accession_no}: missing <reportingOwner>")
    owner_id = owner.find("reportingOwnerId")
    insider_cik = _pad_cik(_text(owner_id.find("rptOwnerCik")) if owner_id is not None else None)
    insider_name = (
        _text(owner_id.find("rptOwnerName")) if owner_id is not None else None
    ) or ""

    rel = owner.find("reportingOwnerRelationship")
    is_director = _bool_value(rel, "isDirector")
    is_officer = _bool_value(rel, "isOfficer")
    is_ten_percent_owner = _bool_value(rel, "isTenPercentOwner")
    officer_title = None
    if rel is not None:
        officer_title = _text(rel.find("officerTitle")) or _value_text(rel, "officerTitle")

    footnote_blob = _collect_footnote_text(root)
    is_10b5_1 = bool(_RULE_10B5_1.search(footnote_blob))

    non_deriv = root.find("nonDerivativeTable")
    if non_deriv is None:
        # No non-derivative transactions in this filing. Return empty list
        # rather than raise — derivative-only filings are valid Form 4s, we
        # just don't capture them.
        return []

    out: list[Form4Filing] = []
    for txn in non_deriv.findall("nonDerivativeTransaction"):
        txn_date = _parse_date(
            _value_text(txn, "transactionDate"),
            context=f"{accession_no} transactionDate",
        )

        coding = txn.find("transactionCoding")
        code = _text(coding.find("transactionCode")) if coding is not None else None
        if not code:
            logger.debug("%s: transaction missing code, skipping", accession_no)
            continue

        amounts = txn.find("transactionAmounts")
        shares = _parse_decimal(_value_text(amounts, "transactionShares"))
        price = _parse_decimal(_value_text(amounts, "transactionPricePerShare"))
        # Shares are always recorded positive; direction is encoded in
        # the transactionCode (P=purchase, S=sale, A=grant, M=exercise, ...).
        shares = abs(shares)
        value_usd = (shares * price).quantize(Decimal("0.0001"))

        post = txn.find("postTransactionAmounts")
        shares_after = _parse_decimal(_value_text(post, "sharesOwnedFollowingTransaction"))

        # Per-transaction 10b5-1 marker: some filings tag the individual
        # transaction via <transactionTimeliness> or a v1Indicator. The
        # primary signal lives in footnotes, but check the coding block too.
        local_blob = ""
        if coding is not None:
            local_blob = " ".join(t for t in coding.itertext() if t)
        txn_10b5_1 = is_10b5_1 or bool(_RULE_10B5_1.search(local_blob))

        out.append(
            Form4Filing(
                accession_no=accession_no,
                filing_date=filing_date,
                transaction_date=txn_date,
                ticker=ticker,
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                insider_cik=insider_cik,
                insider_name=insider_name,
                insider_title=officer_title,
                is_officer=is_officer,
                is_director=is_director,
                is_ten_percent_owner=is_ten_percent_owner,
                transaction_code=code,
                shares=shares,
                price_per_share=price,
                value_usd=value_usd,
                is_10b5_1=txn_10b5_1,
                shares_owned_after=shares_after,
            )
        )
    return out
