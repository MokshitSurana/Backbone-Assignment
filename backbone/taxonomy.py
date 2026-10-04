"""Canonical vocabularies and the source-authority policy.

Everything here is data, not patient facts. Changing a rule here changes how the
whole corpus reconciles, which is why the rule ids are stored on every decision.
"""
from __future__ import annotations

EXTRACTOR_VERSION = "1.1.0"

# --------------------------------------------------------------------------
# Service taxonomy
# --------------------------------------------------------------------------
# Canonical service types. `therapy_candidates` are the only ones that can ever
# contribute to a therapy count; whether they actually do is decided by the
# treatment plan's own inclusion language (see plan.py) plus patient presence.
SERVICE_TYPES = (
    "individual_therapy",
    "group_therapy",
    "family_therapy",
    "medication_management",
    "care_coordination",
    "collateral_contact",
    "measurement_review",
    "scheduling_admin",
    "authorization",
    "billing",
    "unknown",
)

THERAPY_CANDIDATES = ("individual_therapy", "group_therapy", "family_therapy")

# Longest phrase wins, so order matters: checked longest-first at runtime.
SERVICE_SYNONYMS = {
    "individual_therapy": [
        "individual psychotherapy", "individual therapy", "patient-present individual therapy",
        "individual session", "individual visit", "individual contact", "individual appointment",
        "individual", "1:1 therapy", "one to one therapy",
    ],
    "group_therapy": [
        "group psychotherapy", "coping skills group", "skills group", "therapeutic group",
        "process group", "group therapy", "group session", "group work", "group", "gt",
    ],
    "family_therapy": [
        "family psychotherapy", "family therapy", "family session", "family service",
        "family appointment", "couples therapy", "family visit", "family",
    ],
    "medication_management": [
        "medication management", "medication review and management", "medication review",
        "prescriber visit", "med management", "medication evaluation", "medication appointment",
    ],
    "care_coordination": [
        "care coordination", "case conference", "coordination call", "treatment team meeting",
    ],
    "collateral_contact": [
        "family collateral", "partner collateral", "collateral contact", "collateral discussion",
        "collateral",
    ],
    "measurement_review": [
        "measurement review", "symptom measure review", "symptom questionnaire and chart review",
        "questionnaire review", "measure review",
    ],
    "scheduling_admin": [
        "scheduling support log", "program operations", "appointment desk", "records inbox",
        "administrative import receipt", "administrative chart extract", "scheduling",
    ],
    "authorization": ["authorization", "administrative correspondence"],
    "billing": ["charge extract", "posted charge", "billing", "charge"],
}


def canonical_service(text: str | None) -> str:
    """Map free-text service wording to a canonical type. Longest phrase wins."""
    if not text:
        return "unknown"
    low = " " + text.lower().replace("–", "-").replace("—", "-") + " "
    best: tuple[int, str] = (0, "unknown")
    for canon, phrases in SERVICE_SYNONYMS.items():
        for p in phrases:
            if p in low and len(p) > best[0]:
                best = (len(p), canon)
    return best[1]


# --------------------------------------------------------------------------
# Document classes and authority
# --------------------------------------------------------------------------
# authority_class drives the per-field conflict policy in reconcile.py.
#
#   clinical_note       signed, patient-specific narrative by a treating clinician
#   attendance_register signed attendance/disposition record from the desk
#   correction          an entry that names the record and field it replaces
#   retransmission      a resend/copy of an earlier record; carries no new authority
#   schedule_export     booking view; proves a booking, never delivery
#   platform_log        telehealth connection export
#   draft_note          unsigned, template-populated text
#   billing             charge/claim rows
#   authorization       payer approval letters
#   admin_log           scheduling calls, cancellation notices, import receipts
#   measure             symptom questionnaire submission/review
#   treatment_plan      the plan that sets requirements
DOC_CLASSES = (
    "clinical_note", "attendance_register", "correction", "retransmission",
    "schedule_export", "platform_log", "draft_note", "billing", "authorization",
    "admin_log", "measure", "treatment_plan", "unknown",
)

# Can this class, on its own, establish that patient-present care was delivered?
ESTABLISHES_DELIVERY = {
    "clinical_note": True,
    "attendance_register": True,
    "correction": True,          # only for the field it corrects
    "platform_log": False,       # connection != therapy; corroborates intervals only
    "retransmission": False,     # inherits its original; see reconcile RULE-DEDUPE-02
    "schedule_export": False,
    "draft_note": False,
    "billing": False,
    "authorization": False,
    "admin_log": False,
    "measure": False,
    "treatment_plan": False,
    "unknown": False,
}

# Field-level precedence. Higher wins. Ties are escalated to a dispute, never
# broken silently.
FIELD_AUTHORITY = {
    "presence": {
        "correction": 100, "attendance_register": 80, "clinical_note": 70,
        "platform_log": 40, "admin_log": 35, "retransmission": 30,
        "schedule_export": 20, "draft_note": 5, "billing": 0, "authorization": 0,
        "measure": 0, "treatment_plan": 0, "unknown": 1,
    },
    # Duration: the treating clinician's own patient-contact interval outranks the
    # front-desk badge times, except for group services where the desk owns
    # arrival/departure (see reconcile.RULE-DUR-01).
    "duration": {
        "correction": 100, "clinical_note": 80, "attendance_register": 75,
        "platform_log": 50, "retransmission": 30, "schedule_export": 20,
        "admin_log": 10, "draft_note": 5, "billing": 0, "authorization": 0,
        "measure": 0, "treatment_plan": 0, "unknown": 1,
    },
}

# A document's class comes from its own masthead -- the type line a records
# department prints at the top -- not from any passing mention of a service in
# the body. "...ways to support the treatment plan" inside a collateral note
# must not make that note a treatment plan.
DOC_CLASS_HINTS = {
    # corrections and copies
    "attendance correction": "correction",
    "records inbox cover sheet": "retransmission",
    "retransmission": "retransmission",
    # attendance and schedules
    "outpatient attendance and appointment disposition": "attendance_register",
    "attendance and appointment disposition": "attendance_register",
    "patient-specific attendance roster": "attendance_register",
    "group desk attendance": "attendance_register",
    "attendance roster extract": "attendance_register",
    "attendance roster": "attendance_register",
    "attendance register": "attendance_register",
    "attendance extract": "attendance_register",
    "appointment status export": "schedule_export",
    "appointment desk": "schedule_export",
    "platform connection export": "platform_log",
    # clinical narrative
    "skills group clinical record": "clinical_note",
    "skills group activity record": "clinical_note",
    "participating clinician psychotherapy record": "clinical_note",
    "individual psychotherapy": "clinical_note",
    "family psychotherapy": "clinical_note",
    "group psychotherapy": "clinical_note",
    "coping skills group": "clinical_note",
    "family collateral": "clinical_note",
    "partner collateral": "clinical_note",
    "family service": "clinical_note",
    "care coordination": "clinical_note",
    "medication management": "clinical_note",
    "prescriber visit": "clinical_note",
    "clinical record": "clinical_note",
    "psychotherapy": "clinical_note",
    "collateral": "clinical_note",
    # plans, measures, admin
    "outpatient treatment plan": "treatment_plan",
    "treatment plan": "treatment_plan",
    "symptom questionnaire and chart review": "measure",
    "symptom measure review": "measure",
    "symptom questionnaire": "measure",
    "measurement review": "measure",
    "measurement summary": "measure",
    "measure review": "measure",
    "autogenerated progress note": "draft_note",
    "draft - unsigned": "draft_note",
    "charge extract": "billing",
    "posted charge": "billing",
    "administrative correspondence": "authorization",
    "authorization:": "authorization",
    "scheduling support log": "admin_log",
    "administrative import receipt": "admin_log",
    "administrative chart extract": "admin_log",
    "program operations": "admin_log",
    "import receipt": "admin_log",
}

# Lines that are never a type line. The organisation name is NOT skipped: these
# records print the type after it on the same line ("Harbor Grove Behavioral
# Health | Coping skills group").
_MASTHEAD_SKIP = ("document id", "synthetic training record")


def _hit(haystack: str) -> str | None:
    """Earliest hint in `haystack` wins; the longer phrase breaks a tie.

    Position dominates because the type line comes first: a telehealth note that
    later attaches a connection export is still a clinical note.
    """
    best: tuple[int, int, str] | None = None
    for needle, cls in DOC_CLASS_HINTS.items():
        pos = haystack.find(needle)
        if pos == -1:
            continue
        cand = (pos, -len(needle), cls)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


def classify_document(text: str) -> str:
    low = text.lower().replace("–", "-").replace("—", "-")
    masthead: list[str] = []
    for line in low.splitlines():
        line = line.strip()
        if not line or any(line.startswith(sk) for sk in _MASTHEAD_SKIP):
            continue
        masthead.append(line)
        if len(masthead) >= 8:
            break
    for scope in ("\n".join(masthead), low[:1500], low):
        cls = _hit(scope)
        if cls:
            return cls
    return "unknown"


# --------------------------------------------------------------------------
# Presence vocabulary
# --------------------------------------------------------------------------
# (phrase, presence). Matched by *specificity*, not by position in this list:
# an earlier version returned the first hit, so a note mentioning a
# "cancellation" anywhere was read as cancelled even when it also said
# "attended". The longest matching phrase wins, and when phrases of equal
# specificity disagree the caller is told the document is ambiguous.
PRESENCE_WORDS = [
    ("attended, late arrival", "present"),
    ("cancelled by the clinic", "cancelled"),
    ("patient did not attend", "absent"),
    ("patient cancelled", "cancelled"),
    ("clinic cancelled", "cancelled"),
    ("did not attend", "absent"),
    ("attended part", "partial"),
    ("attended full", "present"),
    ("cancellation", "cancelled"),
    ("was absent", "absent"),
    ("no-show", "absent"),
    ("no show", "absent"),
    ("completed", "present"),
    ("attended", "present"),
    ("present", "present"),
]

#: Which presence values are assertions that care was delivered.
POSITIVE_PRESENCE = ("present", "partial")
NEGATIVE_PRESENCE = ("absent", "cancelled")
