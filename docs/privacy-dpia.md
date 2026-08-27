# Data Protection Impact Assessment

**System:** WariVerse — crowd safety and darshan management, Shri Vitthal-Rukmini
Temple, Pandharpur
**Legal frame:** Digital Personal Data Protection Act, 2023 (India)
**Status:** template completed against the built system. **It is not a legal
sign-off.** Every section marked ⚠️ needs a named human — the Data Fiduciary's
representative — before deployment. A DPIA written entirely by the people who
built the system is a design document, not an assessment.

---

## 1. Why this system processes personal data at all

The Wari brings roughly a million people to a town of a hundred thousand across
eighteen days. The failure this system exists to prevent is a crush, and crush
events correlate with stalled and opposing crowd flow rather than with raw
density alone. Preventing that requires knowing **how many people are in a
space and whether they are moving** — it does not require knowing **who they
are**.

That distinction is the whole architecture, and it is the reason this assessment
is short. The overwhelming majority of what the system processes is not personal
data: a person count and two flow ratios per zone per ten seconds identifies
nobody.

## 2. What is processed

| Data | Personal? | Why | Retention | Where |
| --- | --- | --- | --- | --- |
| Zone person counts, density, flow, stagnation | **No** | Crowd safety | 30d raw, 1y aggregate | `density_readings` |
| Phone number (pass holder, Dindi leader, incident contact) | **Yes** | Identify a booking; reach a person about their own pass or emergency | HMAC indefinitely; **raw encrypted, TTL'd** | `passes.holder_phone_hash`, `contact_secrets` |
| Name, role, password hash (staff) | **Yes** | Authentication and accountability | Employment + 1y | `users` |
| Missing-person report incl. photo | **Yes, sensitive** | Reunite a child with a parent | **30d after case closure, auto-purged** | `missing_persons` |
| Breach evidence clip (10s) | **Yes, incidental** | Prove a gate rule was broken | 90d, auto-purged | `breach_events.clip_uri` |
| Dindi position pings | **No** (see §4) | Halt-town readiness | Season | `dindi_pings` |
| Assistant questions | **Possibly** | Review of AI answers | 90d, phone numbers redacted at write | `assistant_turns` |
| Audit log | **Yes** (actor) | Accountability, non-repudiation | 7y, append-only | `audit_log` |

## 3. What is deliberately NOT processed

This list is the assessment's main finding, and it is enforced in code rather
than in policy:

- **No facial recognition. No gait analysis. No biometric templates.** No model
  in this system is trained on or infers identity. There is no table that could
  hold a biometric template.
- **No cross-camera re-identification.** Tracking IDs are ephemeral, in-memory,
  and discarded after aggregation. The same person walking through three zones
  is three independent counts, not one tracked individual.
- **No video is persisted**, except the 10-second breach clip (§6).
- **No pilgrim location is stored against an identity.** The pilgrim app's map
  works from zone polygons served to the device; position stays on the phone.
- **No raw phone number on any entity row.** Only HMAC-SHA256 with a server-side
  secret.

**Why this matters legally, not just ethically:** it is what keeps the system
outside the categories that would otherwise make deployment by a temple trust
practically impossible under the DPDP Act. Zero-biometric is compliance by
construction (E9), not a control that can be misconfigured.

## 4. The one that looks like tracking and is not

Palkhi tracking (Section 4/M8) follows position over 250 km for 18 days, which
deserves a direct answer.

**A Dindi is a group, not a person.** One designated volunteer phone reports for
a walking group of a few hundred. The stored position is the group's position.
There is no column anywhere in `dindis`, `dindi_pings` or `dindi_schedule` that
identifies an individual walker, and the volunteer carrying the phone is
identified only as the Dindi's registered device id — not by name.

**Assessed residual risk:** the designated volunteer is, in practice, locatable
for the duration of the walk. This is *consented, role-based and disclosable*:
they are a named volunteer performing a role for their own Dindi, they install
the app knowingly, and the Dindi's position is already public — it is a
procession that ten thousand people can see.

⚠️ **Required before deployment:** written consent from each designated
volunteer, in Marathi, stating that their phone reports the group's position for
the duration of the Wari and that they may hand the role to someone else or stop
at any time. Draft copy in §9.

**Mitigations in place:** one device per Dindi, enforced (a second phone is
refused); position is coarse relative to a person (the pace estimate deliberately
averages over 90 minutes); leader phone numbers are HMAC'd on the row and
encrypted with a 210-day TTL; reading a leader's real number is a separate,
permissioned, audited endpoint.

## 5. Phone numbers — the data minimisation core

Section 12's rule, and how it is actually implemented:

1. **HMAC on the entity.** `passes.holder_phone_hash`,
   `dindis.leader_phone_hash`. Keyed with `PHONE_HASH_SECRET`, so the hashes are
   not attackable by enumerating the 10-digit space with a plain hash.
2. **Raw only in `contact_secrets`**, Fernet-encrypted, with `purge_after` set at
   write time and a scheduled purge job that enforces it.
3. **Purpose is a column.** `pass_notification`, `incident_contact`,
   `dindi_leader` — a number stored to reach a pass holder cannot be read by a
   code path looking for a Dindi leader.

⚠️ **Finding — retention disparity requiring sign-off.** Dindi leader contacts
are kept 210 days (a Wari season plus reconciliation), against 30 days for pass
holders. The justification is that a halt-town coordinator may need to reach a
leader at any point across an 18-day walk and during the post-event review,
whereas a pass holder's number is needed only while their pass is live. **This
is a deliberate deviation from the 30-day figure in Section 12 and needs
explicit approval, or the TTL should be cut to the length of the season only.**

## 6. The breach clip — a narrow, controlled exception

The one place video is persisted. The controls, in order:

- **10 seconds**, around the crossing, nothing else.
- **90-day retention**, auto-purged, with a `purge_log` row written on **every**
  run including the empty ones — the evidence that retention has been applied
  all season rather than since last Tuesday.
- **Viewing requires re-authentication**, a purpose note, and writes a
  `clip_access_log` row visible to the Administrator.
- **Hash-chained.** Each record's hash covers the previous one, so removing or
  altering a record is detectable. Verified hourly by a scheduled job; a
  detected break is written to the append-only audit log, so the discovery of
  tampering cannot itself be quietly removed.
- **Redaction, not deletion.** A System Admin can remove a clip with a mandatory
  written reason; the record and its hash remain. This is a deliberate deviation
  from the literal instruction, documented in the README — a ledger whose rows
  can vanish is not a ledger.

**The ethics position, stated plainly and carried into the pitch:** the breach
system's purpose is *accountability of a process, not surveillance of people*.
It records that a rule was broken at a gate at a time. It does not identify who
broke it, and it leaves that question to lawful human process. There is no
column in `breach_events` that identifies a person.

## 7. Rights of Data Principals (DPDP Ch. III)

| Right | How it is served | Status |
| --- | --- | --- |
| Access | Pass holder sees their own pass; `GET /incidents/{id}` returns the reporter's own report | Built |
| Correction | Via the help desk, staff-mediated | ⚠️ No self-service endpoint |
| Erasure | Automatic on retention expiry; on request, staff-mediated | ⚠️ No self-service endpoint |
| Grievance redressal | ⚠️ **Not built.** Needs a named Data Protection Officer and a published channel | ⚠️ **Blocking** |
| Consent withdrawal | Notification opt-out at the app level | ⚠️ Partial |

⚠️ **Blocking finding:** the DPDP Act requires a published grievance mechanism
and a named contact. Neither is a software feature the repository can supply on
its own; both must exist before processing begins.

## 8. Security measures (Section 12, implemented)

- Argon2id password hashing; JWT rotation with refresh-token **reuse detection**
- MFA required for Administrator and System Admin, enforced at the dependency
  layer, not per-route
- Role/permission matrix in exactly one file; routes ask for a permission, never
  a role
- Append-only audit log with a database **trigger** blocking UPDATE and DELETE —
  a grant can be changed by whoever holds the role, a trigger has to be dropped,
  and dropping it is itself a visible schema change
- Strict CORS, CSP (`default-src 'none'`), HSTS in production, `nosniff`,
  `frame-ancestors 'none'`
- Parameterised queries throughout; Pydantic validation on every input
- Rate limiting on OTP (3/hr), booking (5/day) — **never on SOS**, which
  degrades to attaching to the caller's existing incident rather than refusing
- `bandit` and `pip-audit` on every PR; `npm audit` on the front ends
- Secrets from environment; `assert_production_safe()` refuses to boot with
  development defaults for JWT, phone-hash, QR-signing or AI-service secrets

⚠️ **Deployment findings:** TLS termination, at-rest encryption of the Postgres
volume, and key management for `PHONE_HASH_SECRET` / `CONTACT_ENCRYPTION_KEY`
are all deployment concerns this repository does not and cannot solve. Rotating
`PHONE_HASH_SECRET` invalidates every stored hash; rotating
`QR_SIGNING_SECRET` invalidates every pass already in a pilgrim's pocket. Treat
both as permanent for the duration of a Wari.

## 9. Notices to be posted and shown

Section 12 asks for these to be generated. They are in
[`docs/signage.md`](signage.md): CCTV signage in Marathi, Hindi and English, and
the consent copy for location and notification permissions, Marathi first.

⚠️ Both need review by a native Marathi speaker and by the trust's legal adviser
before printing or shipping.

## 10. Summary of findings

**Low residual risk**, on the strength of the architectural constraints in §3 —
these are not controls that can be misconfigured, they are absences.

Outstanding before deployment:

1. ⚠️ **Blocking** — name a Data Protection Officer and publish a grievance
   channel (§7).
2. ⚠️ **Blocking** — written Marathi consent from designated Dindi volunteers
   (§4).
3. ⚠️ Approve or reduce the 210-day Dindi leader contact TTL (§5).
4. ⚠️ Native-speaker and legal review of all notices (§9).
5. ⚠️ Confirm TLS, volume encryption and secret management in the deployment
   environment (§8).
6. ⚠️ Decide whether self-service correction and erasure endpoints are required
   or whether staff-mediated handling suffices (§7).

**Review cadence:** before each Wari, and after any change that adds a column
holding personal data. This document is versioned with the code so that "what
was the system doing in 2026" is answerable.
