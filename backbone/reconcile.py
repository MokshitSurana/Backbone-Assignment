"""Claims -> events.

Two stages, both deterministic:

  match    decide which claims describe the same real-world contact
  resolve  decide each field of that contact, under a stated per-field policy

Every outcome of either stage writes a row to `decisions` naming the rule id, so
a reviewer can ask "why 11:15?" and get an answer that is not "the model
thought so".

Rule ids
  RULE-MATCH-01  an encounter id is the event key
  RULE-MATCH-02  an appointment id resolves to its encounter
  RULE-MATCH-03  a call/session id never keys an event
  RULE-MATCH-04  no id: match on patient + date + service type, confirmed by
                 time overlap when both sides have times
  RULE-MATCH-05  more than one candidate and no tiebreak: ambiguous, left
                 unmatched and reported
  RULE-DEDUPE-02 a retransmission inherits its original and gains no authority
  RULE-SVC-01    service type by weighted vote, therapy types preferred over
                 administrative wording
  RULE-PRES-01   a correction to a presence field wins
  RULE-PRES-02   positive delivery needs a class that can establish delivery
  RULE-PRES-03   absence/cancellation may be established by a final disposition
  RULE-PRES-04   qualified claims disagree: occurred=unknown, disputed
  RULE-DUR-01    group service: the attendance desk owns arrival/departure
  RULE-DUR-02    other services: the treating clinician's patient-contact
                 interval is authoritative
  RULE-DUR-03    equal authority, different values: keep both as min/max bounds
  RULE-DUR-04    breaks reduce a session by interval overlap, never by a flat
                 amount
  RULE-DUR-05    schedule, draft, billing, authorization and retransmission
                 claims contribute no duration
  RULE-INC-01    therapy inclusion follows the treatment plan's own wording
  RULE-INC-02    minutes require the patient to have been present
  RULE-MEAS-01   measure identity = instrument + form id + completion date
"""
from __future__ import annotations

import json

from . import intervals as iv
from .store import Store
from .taxonomy import (ESTABLISHES_DELIVERY, FIELD_AUTHORITY, THERAPY_CANDIDATES,
                       canonical_service)

# Service types that only become an event when a document gives them an
# encounter id; otherwise they are chart furniture, not care.
NON_EVENT_TYPES = {"scheduling_admin", "authorization", "billing",
                   "measurement_review", "unknown", None}

NEGATIVE = {"absent", "cancelled"}
POSITIVE = {"present", "partial"}

POLICIES = ("explicit_correction", "latest_document")


# ---------------------------------------------------------------------------
def reconcile_patient(st: Store, mrn: str, policy: str = "explicit_correction") -> dict:
    assert policy in POLICIES, policy
    # Every decision written from here on is stamped with this patient, so a
    # later rebuild can remove exactly this patient's decisions and an answer
    # can filter to them.
    st.set_patient_context(mrn)
    st.clear_patient_events(mrn)
    claims = [dict(r) for r in st.claims_for_patient(mrn)]
    for c in claims:
        c["intervals"] = json.loads(c["intervals"] or "[]")
        c["breaks"] = json.loads(c["breaks"] or "[]")
        c["fields"] = json.loads(c["fields"] or "{}")

    enc_claims = [c for c in claims if c["claim_type"] in ("encounter", "charge")]
    corr_claims = [c for c in claims if c["claim_type"] == "correction"]

    buckets, unmatched, matched_by = _match(st, mrn, enc_claims)
    for c in corr_claims:
        key = c["encounter_ref"]
        if key and key in buckets:
            buckets[key].append(c)
            matched_by[c["claim_id"]] = "RULE-PRES-01"
            st.decide(_event_id(mrn, key), "match", "claim", c["claim_id"],
                      "RULE-PRES-01",
                      f"correction names encounter {key}; attached to that event",
                      [c["claim_id"]])
        else:
            unmatched.append((c, "correction names an encounter with no other record"))

    stats = {"events": 0, "disputed": 0, "unmatched": len(unmatched),
             "therapy_events": 0, "policy": policy}
    include_types, exclude_types = _inclusion(st, mrn)

    for key, group in sorted(buckets.items()):
        ev = _resolve(st, mrn, key, group, policy, include_types, exclude_types,
                      matched_by)
        stats["events"] += 1
        stats["disputed"] += ev["disputed"]
        stats["therapy_events"] += ev["counts_as_therapy"]

    for c, why in unmatched:
        st.decide(None, "match", "unmatched", c["claim_id"], "RULE-MATCH-05", why,
                  [c["claim_id"]])
    _measures(st, mrn, [c for c in claims if c["claim_type"] == "measure"])
    _observations(st, mrn, [c for c in claims if c["claim_type"] == "observation"])
    _plan_rows(st, mrn, [c for c in claims if c["claim_type"] == "plan_requirement"],
               [c for c in claims if c["claim_type"] == "episode"])
    st.set_patient_context(None)
    st.commit()
    return stats


def _event_id(mrn: str, key: str) -> str:
    return f"{mrn}|{key}"


# ---------------------------------------------------------------------------
# stage 1: matching
# ---------------------------------------------------------------------------
def _match(st: Store, mrn: str, claims: list[dict]):
    buckets: dict[str, list[dict]] = {}
    unmatched: list[tuple[dict, str]] = []
    # claim_id -> the rule that actually matched it, so the audit trail does not
    # claim an encounter id was used when the match came from an appointment id
    # or from the date/service fallback.
    matched_by: dict[str, str] = {}

    # appointment id -> encounter id, learned from claims that carry both
    appt2enc: dict[str, str] = {}
    for c in claims:
        if c["encounter_ref"] and c["appointment_ref"]:
            appt2enc[c["appointment_ref"]] = c["encounter_ref"]

    deferred: list[dict] = []
    for c in claims:
        enc = c["encounter_ref"]
        if enc:
            buckets.setdefault(enc, []).append(c)
            matched_by[c["claim_id"]] = "RULE-MATCH-01"
            st.decide(_event_id(mrn, enc), "match", "claim", c["claim_id"],
                      "RULE-MATCH-01",
                      f"document states encounter {enc}", [c["claim_id"]])
            continue
        if c["appointment_ref"] and c["appointment_ref"] in appt2enc:
            enc = appt2enc[c["appointment_ref"]]
            buckets.setdefault(enc, []).append(c)
            matched_by[c["claim_id"]] = "RULE-MATCH-02"
            st.decide(_event_id(mrn, enc), "match", "claim", c["claim_id"],
                      "RULE-MATCH-02",
                      f"appointment {c['appointment_ref']} belongs to encounter {enc}"
                      + (f"; call id {c['call_ref']} is not an event key (RULE-MATCH-03)"
                         if c["call_ref"] else ""),
                      [c["claim_id"]])
            continue
        deferred.append(c)

    # fallback matching, after every id-keyed bucket exists
    for c in deferred:
        if not c["service_date"]:
            unmatched.append((c, "no encounter id and no service date"))
            continue
        svc = c["service_type"]
        # A document that says of itself that it records no visit must never be
        # matched into a clinical event, even when its filing date happens to
        # fall on a service date. This is what keeps an import receipt or a
        # questionnaire review from voting on whether a session happened.
        if c["fields"].get("negation_kind") == "not_a_contact":
            unmatched.append((c, "document states it records no patient visit; "
                                 "never matched to an encounter"))
            continue
        if svc in NON_EVENT_TYPES and not c["intervals"] and not c["stated_minutes"]:
            unmatched.append((c, f"administrative content ({svc}) with no encounter id"
                                 " and no patient-present time"))
            continue
        cands = []
        for key, group in buckets.items():
            if _group_date(group) != c["service_date"]:
                continue
            gsvc = _vote_service(group)
            if svc and gsvc and svc != gsvc and svc not in NON_EVENT_TYPES:
                continue
            cands.append(key)
        if len(cands) > 1 and c["intervals"]:
            mine = iv.loads(c["intervals"])
            overlapping = [k for k in cands
                           if iv.overlaps(mine, _group_intervals(buckets[k])) > 0]
            if len(overlapping) == 1:
                cands = overlapping
        if len(cands) == 1:
            buckets[cands[0]].append(c)
            matched_by[c["claim_id"]] = "RULE-MATCH-04"
            st.decide(_event_id(mrn, cands[0]), "match", "claim", c["claim_id"],
                      "RULE-MATCH-04",
                      f"no encounter id; matched on date {c['service_date']}"
                      f" and service {svc or 'unspecified'}", [c["claim_id"]])
        elif len(cands) > 1:
            unmatched.append((c, f"ambiguous: {len(cands)} candidate events on "
                                 f"{c['service_date']} ({', '.join(sorted(cands))})"))
        elif svc in NON_EVENT_TYPES and not c["intervals"]:
            unmatched.append((c, f"administrative content ({svc}); no encounter created"))
        else:
            key = f"{c['service_date']}|{svc}"
            buckets.setdefault(key, []).append(c)
            matched_by[c["claim_id"]] = "RULE-MATCH-04"
            st.decide(_event_id(mrn, key), "match", "claim", c["claim_id"],
                      "RULE-MATCH-04",
                      "no encounter id and no existing candidate; opened a new event",
                      [c["claim_id"]])
    return buckets, unmatched, matched_by


def _group_date(group: list[dict]) -> str | None:
    dates = [c["service_date"] for c in group if c["service_date"]]
    if not dates:
        return None
    return max(set(dates), key=dates.count)


def _group_intervals(group: list[dict]) -> list[iv.Interval]:
    out: list[iv.Interval] = []
    for c in group:
        out += iv.loads(c["intervals"])
        sched = c["fields"].get("scheduled")
        if sched:
            out += iv.loads([sched])
    return iv.normalize(out)


def _vote_service(group: list[dict]) -> str:
    weights: dict[str, float] = {}
    for c in group:
        svc = c["service_type"]
        if not svc or svc == "unknown":
            continue
        w = 1.0
        if svc in THERAPY_CANDIDATES:
            w += 2.0
        if svc in ("medication_management", "care_coordination", "collateral_contact"):
            w += 2.0
        if c["authority_class"] in ("clinical_note", "attendance_register"):
            w += 1.5
        if c["authority_class"] in ("schedule_export",):
            w += 1.0
        if c["fields"].get("evidence_only"):
            w *= 0.4
        weights[svc] = weights.get(svc, 0) + w
    if not weights:
        return "unknown"
    return max(weights.items(), key=lambda kv: (kv[1], kv[0]))[0]


# ---------------------------------------------------------------------------
# stage 2: resolution
# ---------------------------------------------------------------------------
def _recency(c: dict) -> str:
    return c["received_at"] or c["authored_at"] or c["service_date"] or ""


def _resolve(st: Store, mrn: str, key: str, group: list[dict], policy: str,
             include_types, exclude_types, matched_by: dict | None = None) -> dict:
    eid = _event_id(mrn, key)
    enc_ref = key if key.upper().startswith(("HG-E", "BH-E")) or "-E" in key.upper() else None
    date = _group_date(group)
    svc = _vote_service(group)
    st.decide(eid, "event", "service_type", svc, "RULE-SVC-01",
              "weighted vote over claims; therapy and clinical wording preferred "
              "over administrative wording",
              [c["claim_id"] for c in group if c["service_type"]])

    corrections = [c for c in group if c["claim_type"] == "correction"]
    encs = [c for c in group if c["claim_type"] != "correction"]

    # ---- presence ----------------------------------------------------------
    occurred, present, disputed, pres_note = _presence(st, eid, encs, corrections, policy)

    # ---- duration ----------------------------------------------------------
    breaks: list[iv.Interval] = []
    break_src: list[str] = []
    for c in encs:
        if c["breaks"]:
            breaks += iv.loads(c["breaks"])
            break_src.append(c["claim_id"])
    breaks = iv.normalize(breaks)
    if breaks:
        st.decide(eid, "event", "breaks", json.dumps(iv.dumps(breaks)), "RULE-DUR-04",
                  "nontherapeutic intervals subtracted by overlap with the patient's "
                  "own presence interval, not as a flat amount", break_src)

    lo = hi = 0
    dur_disputed = False
    chosen_from: list[str] = []
    pres_ivs: list = []
    if occurred == "yes" and present in ("yes", "partial"):
        lo, hi, dur_disputed, chosen_from, pres_ivs = _duration(
            st, eid, svc, encs, corrections, breaks, policy)
    elif occurred == "unknown" and present != "no":
        # An unresolved attendance is not zero care: it is somewhere between
        # none and whatever the records describe. Computing the duration anyway
        # and keeping it as the UPPER bound only is what lets a week whose
        # result hinges on a disputed attendance come out CANNOT_DETERMINE
        # rather than NOT_MET -- and what lets such a patient reach the
        # "inclusion depends on unresolved documentation" group.
        _, hi, dur_disputed, chosen_from, pres_ivs = _duration(
            st, eid, svc, encs, corrections, breaks, policy)
        lo = 0
        if hi:
            st.decide(eid, "event", "minutes", f"0-{hi}", "RULE-PRES-04",
                      f"attendance is unresolved, so the minutes are bounded by "
                      f"[0, {hi}]: nothing is established, and {hi} is what the "
                      f"records would support if the contact did occur",
                      chosen_from)
    disputed = disputed or dur_disputed

    # ---- therapy inclusion -------------------------------------------------
    counts, why = _counts(svc, occurred, present, include_types, exclude_types)
    st.decide(eid, "event", "counts_as_therapy", "yes" if counts else "no",
              "RULE-INC-01" if svc not in include_types else "RULE-INC-02", why,
              [c["claim_id"] for c in encs])

    resolved = {
        "service_type": svc, "service_date": date, "occurred": occurred,
        "patient_present": present, "min_minutes": lo, "max_minutes": hi,
        "breaks": iv.dumps(breaks), "presence_note": pres_note,
        "presence_intervals": pres_ivs, "duration_claims": chosen_from,
        "evidence_docs": sorted({c["doc_id"] or c["filename"] for c in group}),
    }
    ev = {
        "event_id": eid, "patient_mrn": mrn, "encounter_ref": enc_ref,
        "service_date": date or "", "service_type": svc, "occurred": occurred,
        "patient_present": present, "min_minutes": lo, "max_minutes": hi,
        "counts_as_therapy": 1 if counts else 0,
        "exclusion_reason": None if counts else why,
        "disputed": 1 if disputed else 0,
        "resolved": json.dumps(resolved, sort_keys=True),
        "match_rule": "RULE-MATCH-01" if enc_ref else "RULE-MATCH-04",
    }
    if not date:
        st.decide(eid, "event", "service_date", None, "RULE-DATE-01",
                  "no claim states a usable service date for this encounter; the "
                  "event is retained but excluded from every weekly total",
                  [c["claim_id"] for c in group])
    st.insert_event(ev)
    for c in group:
        st.link_claim(eid, c["claim_id"], _role(c, chosen_from),
                      (matched_by or {}).get(c["claim_id"], "RULE-MATCH-04"))
    return ev


def _role(c: dict, chosen_from: list[str]) -> str:
    if c["claim_type"] == "correction":
        return "correction"
    if c["claim_id"] in chosen_from:
        return "primary"
    if c["authority_class"] == "retransmission":
        return "superseded"
    if c["authority_class"] in ("draft_note", "billing", "authorization"):
        return "rejected"
    return "corroborating"


def _presence(st: Store, eid: str, encs: list[dict], corrections: list[dict],
              policy: str):
    note = ""
    pc = [c for c in corrections if c["fields"].get("target_field") == "presence"]
    if pc:
        c = pc[-1]
        val = (c["fields"].get("new_value") or "").lower()
        occurred = "no" if any(w in val for w in ("no show", "cancel", "absent")) else "yes"
        st.decide(eid, "event", "occurred", occurred, "RULE-PRES-01",
                  f"explicit correction sets attendance to {val!r}", [c["claim_id"]])
        return occurred, ("no" if occurred == "no" else "yes"), False, "corrected"

    qualified_pos, qualified_neg, rejected = [], [], []
    for c in encs:
        amb = c["fields"].get("presence_ambiguous")
        if amb and ESTABLISHES_DELIVERY.get(c["authority_class"], False):
            # The document says both things. Record it and let the claim stand
            # on its chosen cue; if another record disagrees the usual dispute
            # rules apply, and the reader can see why this one was uncertain.
            st.decide(eid, "event", "presence_cue_ambiguous",
                      f"{amb['chosen']}/{amb['competing']}", "RULE-PRES-05",
                      f"{c['doc_id'] or c['filename']} contains both "
                      f"{amb['chosen']!r} ({amb['chosen_presence']}) and "
                      f"{amb['competing']!r} ({amb['competing_presence']}); the "
                      f"more specific phrase was used", [c["claim_id"]])
        if c["presence"] in POSITIVE:
            if ESTABLISHES_DELIVERY.get(c["authority_class"], False):
                qualified_pos.append(c)
            else:
                rejected.append(c)
        elif c["presence"] in NEGATIVE:
            if c["authority_class"] in ("attendance_register", "schedule_export",
                                  "admin_log", "clinical_note", "correction"):
                qualified_neg.append(c)
            else:
                rejected.append(c)

    if policy == "latest_document":
        pool = [c for c in encs if c["presence"] in POSITIVE | NEGATIVE
                and c["authority_class"] not in ("draft_note", "billing")]
        if pool:
            winner = max(pool, key=_recency)
            occurred = "no" if winner["presence"] in NEGATIVE else "yes"
            st.decide(eid, "event", "occurred", occurred, "POLICY-LATEST",
                      f"latest-document policy: {winner['doc_id']} "
                      f"({_recency(winner)}) states {winner['presence']}",
                      [winner["claim_id"]])
            present = "no" if occurred == "no" else (
                "no" if winner["fields"].get("presence_cue", "").startswith("absent")
                else "yes")
            return occurred, present, False, "latest-document policy"

    for c in rejected:
        st.decide(eid, "event", "rejected_presence", c["presence"], "RULE-PRES-02",
                  f"{c['authority_class']} cannot establish that care was delivered "
                  f"({c['doc_id'] or c['filename']})", [c["claim_id"]])

    if qualified_pos and qualified_neg:
        st.decide(eid, "event", "occurred", "unknown", "RULE-PRES-04",
                  "qualified records disagree about whether the contact happened",
                  [c["claim_id"] for c in qualified_pos + qualified_neg])
        return "unknown", "unknown", True, "qualified records disagree"
    if qualified_neg:
        kinds = {c["fields"].get("negation_kind") for c in qualified_neg}
        c = qualified_neg[0]
        cue = c["fields"].get("presence_cue") or c["presence"]
        # A no-show or cancellation means no contact at all. "The patient was
        # absent for the entire contact" means the contact happened without them,
        # which is a different fact and is why collateral and coordination
        # contacts exist in the record at all.
        if kinds & {"no_show", "cancelled"} or not kinds - {None}:
            st.decide(eid, "event", "occurred", "no", "RULE-PRES-03",
                      f"final disposition records {c['presence']} "
                      f"({c['doc_id'] or c['filename']}: {cue})",
                      [c["claim_id"] for c in qualified_neg])
            return "no", "no", False, cue
        st.decide(eid, "event", "occurred", "yes", "RULE-PRES-03",
                  f"contact is documented but the patient was not present "
                  f"({c['doc_id'] or c['filename']}: {cue})",
                  [c["claim_id"] for c in qualified_neg])
        return "yes", "no", False, cue
    if qualified_pos:
        # No "patient absent" test here: qualified_pos holds only claims whose
        # presence is in POSITIVE, so such a test could never fire. A contact
        # that happened *without* the patient arrives as a qualified negative
        # with negation_kind="patient_absent" and is handled above.
        st.decide(eid, "event", "occurred", "yes", "RULE-PRES-02",
                  "attested or clinically documented patient contact: "
                  + ", ".join(sorted({c["doc_id"] or c["filename"]
                                      for c in qualified_pos})),
                  [c["claim_id"] for c in qualified_pos])
        return "yes", "yes", False, note
    if rejected:
        st.decide(eid, "event", "occurred", "unknown", "RULE-PRES-02",
                  "the only records asserting this contact cannot establish delivery",
                  [c["claim_id"] for c in rejected])
        return "unknown", "unknown", True, "no qualifying record"
    st.decide(eid, "event", "occurred", "unknown", "RULE-PRES-04",
              "no record states attendance for this encounter", [c["claim_id"] for c in encs])
    return "unknown", "unknown", True, "no attendance statement"


def _apply_corrections(c: dict, corrections: list[dict],
                       log=None) -> tuple[list, list[str]]:
    """The claim's patient-present intervals with corrections applied.

    A correction that states the value it replaces is applied to the interval
    carrying that value. A correction that states no old value is only applied
    when the claim has exactly one interval, because otherwise there is no way
    to tell which interval it meant -- rewriting all of them, as an earlier
    version did, would silently corrupt a split session.
    """
    ivs = [list(x) for x in c["intervals"]]
    used: list[str] = []
    for corr in corrections:
        f = corr["fields"]
        field, new = f.get("target_field"), f.get("new_value")
        old = f.get("old_value")
        if not new or field not in ("arrival", "departure"):
            continue
        idx = 0 if field == "arrival" else 1
        targets = [p for p in ivs if p[idx] == old] if old is not None else ivs
        if old is None and len(ivs) > 1:
            if log is not None:
                log.append((corr["claim_id"],
                            f"correction states no previous {field} and the "
                            f"record holds {len(ivs)} intervals; not applied, "
                            f"because which interval it refers to is ambiguous"))
            continue
        if not targets:
            if log is not None:
                log.append((corr["claim_id"],
                            f"correction replaces {field}={old!r}, which this "
                            f"record does not contain; not applied"))
            continue
        for pair in targets:
            pair[idx] = new
            used.append(corr["claim_id"])
    return ivs, used


def _duration(st: Store, eid: str, svc: str, encs: list[dict],
              corrections: list[dict], breaks, policy: str):
    """Minute bounds for one event, with the rule that produced them."""
    cands: list[tuple[int, dict, str, list[str]]] = []
    for c in encs:
        # Under latest_document the only filter is "not a draft and not a
        # charge": every other record competes on recency alone. Keeping the
        # delivery-authority filter here would smuggle the shipped policy into
        # its own baseline and make the comparison meaningless.
        if policy == "latest_document":
            if c["authority_class"] in ("draft_note", "billing", "authorization"):
                continue
        elif not ESTABLISHES_DELIVERY.get(c["authority_class"], False):
            if c["intervals"] or c["stated_minutes"]:
                st.decide(eid, "event", "rejected_duration",
                          str(c["stated_minutes"] or c["intervals"]), "RULE-DUR-05",
                          f"{c['authority_class']} contributes no duration "
                          f"({c['doc_id'] or c['filename']})", [c["claim_id"]])
            continue
        if c["fields"].get("evidence_only"):
            continue
        # Under latest_document a correction is just an older document: it
        # competes on recency like any other and is not applied as an override.
        corr_log: list[tuple[str, str]] = []
        ivs, used = _apply_corrections(
            c, [] if policy == "latest_document" else corrections, corr_log)
        for cid, why in corr_log:
            st.decide(eid, "event", "correction_not_applied", cid,
                      "RULE-PRES-01", why, [cid, c["claim_id"]])
        if ivs:
            resolved_ivs = iv.dumps(iv.subtract(iv.loads(ivs), breaks))
            minutes = iv.present_minutes(iv.loads(ivs), breaks)
            basis = "intervals" + ("+correction" if used else "")
        elif c["stated_minutes"]:
            resolved_ivs = []
            minutes = int(c["stated_minutes"])
            basis = "stated_minutes"
        else:
            continue
        cands.append((minutes, c, basis, used, resolved_ivs))

    # A stated figure with no interval says nothing about whether a recorded
    # break is already netted out of it. Subtracting would risk double-counting
    # the break; asserting the figure would risk counting nontherapeutic time.
    # So the value becomes a band, and the event is marked disputed.
    break_total = iv.total(breaks)
    if break_total and cands and all(t[2] == "stated_minutes" for t in cands):
        vals = sorted({t[0] for t in cands})
        lo_b, hi_b = max(0, vals[0] - break_total), vals[-1]
        st.decide(eid, "event", "minutes", f"{lo_b}-{hi_b}", "RULE-DUR-04",
                  f"the only duration available is a stated figure "
                  f"({'/'.join(str(v) for v in vals)} min) and the record also "
                  f"documents {break_total} min of nontherapeutic break without "
                  f"saying whether the figure already excludes it; kept as bounds",
                  [t[1]["claim_id"] for t in cands])
        return (lo_b, hi_b, True, [t[1]["claim_id"] for t in cands], [])

    if not cands:
        st.decide(eid, "event", "minutes", "0", "RULE-DUR-05",
                  "no qualifying record states a patient-present duration", [])
        return 0, 0, True, [], []

    # authority ranking
    def rank(c: dict) -> float:
        base = FIELD_AUTHORITY["duration"].get(c["authority_class"], 1)
        if svc == "group_therapy" and c["authority_class"] == "attendance_register":
            base += 20                      # RULE-DUR-01
        if svc != "group_therapy" and c["authority_class"] == "clinical_note":
            base += 20                      # RULE-DUR-02
        return base

    if policy == "latest_document":
        best = max(cands, key=lambda t: _recency(t[1]))
        st.decide(eid, "event", "minutes", str(best[0]), "POLICY-LATEST",
                  f"latest-document policy: {best[1]['doc_id']} "
                  f"({_recency(best[1])}) -> {best[0]} min", [best[1]["claim_id"]])
        return best[0], best[0], False, [best[1]["claim_id"]], best[4]

    top = max(rank(t[1]) for t in cands)
    winners = [t for t in cands if rank(t[1]) == top]
    vals = sorted({t[0] for t in winners})
    rule = ("RULE-DUR-01" if svc == "group_therapy" else "RULE-DUR-02")
    if len(vals) == 1:
        w = winners[0]
        st.decide(eid, "event", "minutes", str(vals[0]), rule,
                  f"{w[1]['authority_class']} {w[1]['doc_id'] or w[1]['filename']} is the "
                  f"authority for duration on a {svc} event; {w[2]}"
                  + (f"; correction {','.join(sorted(set(w[3])))} applied" if w[3] else ""),
                  [t[1]["claim_id"] for t in winners])
        return (vals[0], vals[0], False, [t[1]["claim_id"] for t in winners],
                w[4])

    st.decide(eid, "event", "minutes", f"{vals[0]}-{vals[-1]}", "RULE-DUR-03",
              "records of equal authority disagree and neither is a correction: "
              "kept as bounds (" + "; ".join(
                  f"{t[1]['doc_id'] or t[1]['filename']}={t[0]}min" for t in winners)
              + ")", [t[1]["claim_id"] for t in winners])
    widest = max(winners, key=lambda t: t[0])
    return (vals[0], vals[-1], True, [t[1]["claim_id"] for t in winners],
            widest[4])


def _counts(svc, occurred, present, include_types, exclude_types):
    if svc in exclude_types:
        return False, f"{svc} is named in the plan as not contributing"
    if svc not in include_types:
        return False, f"{svc} is not a service type the plan counts toward the goal"
    if occurred != "yes":
        return False, f"contact did not occur (occurred={occurred})"
    if present not in ("yes", "partial"):
        return False, "patient was not present for this contact"
    return True, "patient-present therapy of a type the plan counts"


# ---------------------------------------------------------------------------
# side tables
# ---------------------------------------------------------------------------
def _inclusion(st: Store, mrn: str):
    include, exclude = set(), set()
    for r in st.requirements(mrn):
        include |= set(json.loads(r["service_types"]))
    rows = st.q("SELECT fields FROM claims WHERE patient_mrn=? AND claim_type='plan_requirement'",
                (mrn,))
    for r in rows:
        f = json.loads(r["fields"])
        include |= set(f.get("service_types") or [])
        exclude |= set(f.get("excluded_types") or [])
    if not include:
        include = set(THERAPY_CANDIDATES)
    return include - exclude, exclude


def _measures(st: Store, mrn: str, claims: list[dict]) -> None:
    # Pass 1: learn the form id each (instrument, completion date) is known by,
    # so a re-import that omits the form id still collapses onto the original
    # administration instead of becoming a second one.
    form_for: dict[tuple[str, str], str] = {}
    for c in claims:
        inst = ((c["fields"].get("instrument") or "UNKNOWN")).upper()
        if c["form_ref"] and c["service_date"]:
            form_for[(inst, c["service_date"])] = c["form_ref"]
    for r in st.q("SELECT instrument, completed_at, form_ref FROM measures"
                  " WHERE patient_mrn=? AND form_ref IS NOT NULL", (mrn,)):
        form_for.setdefault((r["instrument"], r["completed_at"]), r["form_ref"])

    for c in claims:
        f = c["fields"]
        inst = (f.get("instrument") or "UNKNOWN").upper()
        completed = c["service_date"] or ""
        form = c["form_ref"] or form_for.get((inst, completed))
        mid = f"{mrn}|{inst}|{form or '-'}|{completed}"
        conflict, totals = st.upsert_measure({
            "measure_id": mid, "patient_mrn": mrn, "instrument": inst,
            "form_ref": form, "completed_at": completed,
            "total": f.get("total"),
            "items": json.dumps(f.get("items")) if f.get("items") else None,
            "claim_ids": json.dumps([c["claim_id"]]),
        })
        if conflict:
            st.decide(None, "measure", "total_conflict",
                      "/".join(f"{t:g}" for t in totals if t is not None),
                      "RULE-MEAS-02",
                      f"two records of the same {inst} administration "
                      f"({completed}) state different scores "
                      + "/".join(f"{t:g}" for t in totals if t is not None)
                      + "; both are kept and the measure is flagged, because "
                        "nothing in the record says which is right",
                      [c["claim_id"]], mrn)
        st.decide(None, "measure", "identity", mid, "RULE-MEAS-01",
                  f"{inst} identified by form {form or 'none'} and completion "
                  f"date {completed}"
                  + ("; this record omits the form id, which was taken from "
                     "another record of the same instrument and completion date"
                     if form and not c["form_ref"] else "")
                  + ("; administrative re-import collapses onto the original"
                     if f.get("is_import") else ""),
                  [c["claim_id"]])


def _observations(st: Store, mrn: str, claims: list[dict]) -> None:
    for c in claims:
        f = c["fields"]
        for dom in f.get("domains", ["general"]):
            st.insert_observation({
                "obs_id": f"{c['claim_id']}|{dom}", "patient_mrn": mrn,
                "obs_date": c["service_date"] or "", "domain": dom,
                "polarity": f.get("polarity", "neutral"),
                "reporter": f.get("reporter", "clinician"),
                "claim_id": c["claim_id"], "quote": c["quote"],
                "quote_start": c["quote_start"], "quote_end": c["quote_end"],
            })


def _plan_rows(st: Store, mrn: str, reqs: list[dict], eps: list[dict]) -> None:
    for c in eps:
        f = c["fields"]
        if f.get("start"):
            st.ex("INSERT OR REPLACE INTO episodes(patient_mrn,start_date,end_date,claim_id)"
                  " VALUES (?,?,?,?)", (mrn, f["start"], f.get("end"), c["claim_id"]))
    # order plans by effective start so a later plan closes the earlier one
    reqs = sorted(reqs, key=lambda c: (c["fields"].get("effective_start") or "",
                                       c["fields"].get("metric") or ""))
    by_metric: dict[str, list[dict]] = {}
    for c in reqs:
        by_metric.setdefault(c["fields"]["metric"], []).append(c)
    for metric, group in by_metric.items():
        for i, c in enumerate(group):
            f = c["fields"]
            end = f.get("effective_end")
            if i + 1 < len(group):
                nxt_start = group[i + 1]["fields"].get("effective_start")
                if nxt_start:
                    end = _day_before(nxt_start)
            st.insert_requirement({
                "req_id": f"{mrn}|{metric}|{f.get('effective_start')}",
                "patient_mrn": mrn, "metric": metric,
                "service_types": json.dumps(f.get("service_types") or []),
                "period": f.get("period", "week_mon_sun"),
                "comparator": f.get("comparator", ">="),
                "threshold": float(f["threshold"]),
                "effective_start": f.get("effective_start") or "",
                "effective_end": end, "claim_id": c["claim_id"],
            })
            st.decide(None, "plan", metric,
                      f"{f.get('comparator', '>=')} {f['threshold']} per {f.get('period')}",
                      "RULE-INC-01",
                      f"requirement read from the treatment plan, effective "
                      f"{f.get('effective_start')} to {end or 'open'}", [c["claim_id"]])


def _day_before(iso: str) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()


def reconcile_all(st: Store, policy: str = "explicit_correction") -> dict:
    out = {}
    for mrn in st.patients():
        out[mrn] = reconcile_patient(st, mrn, policy)
    return out
