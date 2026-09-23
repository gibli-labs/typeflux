"""Generator for the fully synthetic ``sample-contract.pdf`` fixture.

Every party, term, and figure below is fictional, authored for this
repository (see docs/licensing.md). Regenerate with (reportlab pinned so the
``invariant=True`` output is byte-reproducible across machines):

    uv run --with 'reportlab==5.0.1' python examples/contract_risk_review/fixtures/generate_sample_contract.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUT = Path(__file__).resolve().parent / "sample-contract.pdf"

SECTIONS: list[tuple[str, str]] = [
    (
        "MASTER SERVICES AGREEMENT",
        "This Master Services Agreement (the “Agreement”) is entered into as of "
        "March 3, 2026 (the “Effective Date”) by and between Northwind Analytics "
        "Ltd., a fictional company organized under the laws of the State of Delaware "
        "(“Provider”), and Contoso Logistics LLC, a fictional limited liability "
        "company organized under the laws of the State of Washington (“Customer”). "
        "Provider and Customer are each a “Party” and together the “Parties.” "
        "This document is a synthetic fixture created for software testing; it is not a "
        "real agreement and does not describe real entities.",
    ),
    (
        "1. Services",
        "Provider shall deliver the data-pipeline integration and reporting services "
        "described in one or more mutually executed statements of work (each an "
        "“SOW”). Provider shall perform the Services in a professional and "
        "workmanlike manner consistent with generally accepted industry standards. "
        "Deliverables are accepted upon Customer's written approval or thirty (30) days "
        "after delivery if no rejection notice is received, whichever occurs first.",
    ),
    (
        "2. Term and Termination",
        "The initial term of this Agreement is twenty-four (24) months from the Effective "
        "Date, renewing automatically for successive twelve (12) month periods unless "
        "either Party gives written notice of non-renewal at least sixty (60) days before "
        "the end of the then-current term. Either Party may terminate for material breach "
        "upon thirty (30) days written notice if the breach remains uncured. Customer may "
        "terminate for convenience upon ninety (90) days written notice, subject to payment "
        "of fees for Services performed through the termination date plus a wind-down fee "
        "equal to fifteen percent (15%) of the average monthly fees of the prior quarter.",
    ),
    (
        "3. Fees and Payment",
        "Customer shall pay Provider a base subscription fee of $18,500 per month, plus "
        "usage fees of $0.042 per processed record above the monthly included volume of "
        "two million (2,000,000) records. Invoices are issued monthly in arrears and are "
        "payable net forty-five (45) days. Late amounts accrue interest at one percent "
        "(1%) per month or the maximum rate permitted by law, whichever is lower. Fees "
        "increase by no more than four percent (4%) per renewal term with sixty (60) days "
        "prior notice.",
    ),
    (
        "4. Service Levels",
        "Provider shall maintain 99.5% monthly platform availability, measured excluding "
        "scheduled maintenance windows announced at least seventy-two (72) hours in "
        "advance. If availability falls below 99.5% in a calendar month, Customer is "
        "entitled to a service credit of five percent (5%) of that month's base fee per "
        "full percentage point of shortfall, capped at fifty percent (50%) of the monthly "
        "base fee. Service credits are Customer's sole and exclusive remedy for "
        "availability shortfalls.",
    ),
    (
        "5. Data Protection",
        "Each Party shall comply with applicable data-protection laws. Provider shall "
        "process Customer personal data only on documented instructions, maintain "
        "administrative, physical, and technical safeguards no less protective than "
        "industry standard, and notify Customer of a confirmed security incident affecting "
        "Customer data without undue delay and in no event later than seventy-two (72) "
        "hours after confirmation. Provider shall not retain Customer data more than "
        "ninety (90) days after termination, except as required by law.",
    ),
    (
        "6. Intellectual Property",
        "Each Party retains all right, title, and interest in its pre-existing "
        "intellectual property. Provider grants Customer a non-exclusive, non-transferable "
        "license to use the deliverables for Customer's internal business purposes during "
        "the term. Customer grants Provider a limited license to process Customer data "
        "solely to provide the Services. Feedback may be used by Provider without "
        "restriction, provided it contains no Customer Confidential Information.",
    ),
    (
        "7. Confidentiality",
        "Each Party shall protect the other Party's Confidential Information with at least "
        "the same degree of care it uses for its own similar information, and no less than "
        "reasonable care, and shall use it solely to perform under this Agreement. "
        "Confidentiality obligations survive for five (5) years after termination, and for "
        "trade secrets, for as long as trade-secret protection subsists.",
    ),
    (
        "8. Limitation of Liability",
        "EXCEPT FOR BREACHES OF SECTION 7, INDEMNIFICATION OBLIGATIONS, OR A PARTY'S GROSS "
        "NEGLIGENCE OR WILLFUL MISCONDUCT, NEITHER PARTY'S AGGREGATE LIABILITY ARISING OUT "
        "OF OR RELATED TO THIS AGREEMENT SHALL EXCEED THE FEES PAID OR PAYABLE BY CUSTOMER "
        "IN THE TWELVE (12) MONTHS PRECEDING THE EVENT GIVING RISE TO THE CLAIM. NEITHER "
        "PARTY IS LIABLE FOR INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE "
        "DAMAGES, OR FOR LOST PROFITS OR REVENUES.",
    ),
    (
        "9. Indemnification",
        "Provider shall defend and indemnify Customer against third-party claims alleging "
        "that the Services infringe a valid patent, copyright, or trademark, and shall pay "
        "resulting damages finally awarded, provided Customer promptly notifies Provider "
        "and grants sole control of the defense. Customer shall defend and indemnify "
        "Provider against third-party claims arising from Customer data or Customer's use "
        "of the Services in violation of law or this Agreement.",
    ),
    (
        "10. General",
        "This Agreement is governed by the laws of the State of Delaware, excluding its "
        "conflict-of-laws rules. Neither Party may assign this Agreement without the other "
        "Party's prior written consent, except to a successor in a merger or sale of "
        "substantially all assets. Notices must be in writing to the addresses stated in "
        "the applicable SOW. This Agreement, together with its SOWs, is the entire "
        "agreement of the Parties regarding its subject matter and supersedes all prior "
        "discussions. Amendments must be in a writing signed by both Parties.",
    ),
]


def main() -> None:
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("H", parent=styles["Heading2"], spaceAfter=6, keepWithNext=1)
    body = ParagraphStyle("B", parent=styles["BodyText"], leading=14)
    doc = SimpleDocTemplate(
        str(OUT),
        invariant=True,  # deterministic output: fixed timestamps/ID so regeneration is verifiable
        pagesize=LETTER,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        title="Synthetic Master Services Agreement (test fixture)",
        author="Typeflux Temporal fixtures",
        subject="Fully synthetic contract fixture for the contract_risk_review example",
    )
    flow = []
    for title, text in SECTIONS:
        flow.append(Paragraph(title, heading))
        flow.append(Paragraph(text, body))
        flow.append(Spacer(1, 10))
    doc.build(flow)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
