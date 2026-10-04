"""Claim container shared by every extractor."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict


@dataclass
class Claim:
    claim_id: str = ""
    doc_pk: str = ""
    patient_mrn: str | None = None
    claim_type: str = "encounter"
    encounter_ref: str | None = None
    appointment_ref: str | None = None
    form_ref: str | None = None
    call_ref: str | None = None
    service_date: str | None = None
    service_raw: str | None = None
    service_type: str | None = None
    presence: str | None = None
    presence_basis: str | None = None
    arrival: str | None = None
    departure: str | None = None
    intervals: list = field(default_factory=list)      # patient-present [[hh:mm,hh:mm]]
    breaks: list = field(default_factory=list)
    stated_minutes: int | None = None
    minutes_basis: str | None = None
    authority_class: str = "unknown"
    fields: dict = field(default_factory=dict)
    quote: str = ""
    quote_start: int = 0
    quote_end: int = 0
    quote_verified: int = 0
    extractor: str = "rules"

    def row(self) -> dict:
        d = asdict(self)
        d["intervals"] = json.dumps(self.intervals)
        d["breaks"] = json.dumps(self.breaks)
        d["fields"] = json.dumps(self.fields, sort_keys=True)
        return d

    def verify(self, text: str) -> bool:
        ok = text[self.quote_start:self.quote_end] == self.quote
        self.quote_verified = 1 if ok else 0
        return ok


@dataclass
class DocFacts:
    """Document-level facts an extractor returns alongside its claims."""
    patient_mrn: str | None = None
    patient_name: str | None = None
    given_name: str | None = None
    dob: str | None = None
    doc_id: str | None = None
    doc_class: str = "unknown"
    received_at: str | None = None
    authored_at: str | None = None
