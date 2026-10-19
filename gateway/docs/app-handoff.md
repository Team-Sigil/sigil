# App handoff: several dependents per guardian

> Written from [SPEC.md](../SPEC.md) and the [README](../README.md), which are the source of
> truth. If this guide disagrees with them, they win.

For Shine, from the gateway (Julian). Gateway branch `feat/multi-dependent`. The full App API
is in the [README](../README.md) ("App API"); this note covers only what changed.

**Short version:** a guardian can now add more dependents. Guardian screens about one
dependent send an `X-Family-Id` header. **The dependent app does not change.** A guardian
with a single dependent works exactly as before, header or not.

## The model

- **One family per dependent.** Each family has its own policy, weekly budget, freeze,
  questions, policy interview, requests, history and audit trail.
- A dependent has exactly one guardian. A wallet that is already someone's dependent can't
  be added again (`409`).
- **One shared reservoir.** All of a guardian's dependents spend from the same guardian token
  account and the same Phantom delegation.

## 1. After sign-in: which screen

`POST /auth/verify` and `GET /me` now also return `family_ids` (the guardian's families,
oldest first). `family_id` is set only when there is exactly one.

| `role` | `family_ids` | Show |
|---|---|---|
| guardian | `[]` | onboarding: register the first dependent |
| guardian | one id | as today |
| guardian | several | a dependent picker; `GET /families` gives the names |
| dependent | their one id | as today |

`GET /api/v1/families` returns the full family objects (`id`, `dependent_name`,
`dependent_wallet`, `guardian_token_account`, `frozen`, …), oldest first.

## 2. The `X-Family-Id` header

Send `X-Family-Id: <family id>` on every guardian call about one dependent.

| Needs the header (guardian with 2+ dependents) | Never needs it |
|---|---|
| `/family`, `/policy/*` (interview, drafts, activate, active, proposals), `/requests*`, `/questions*`, `/freeze`, `/budget`, `/history`, `/audit`, `/events`, `/capture/devices` | `/auth/*`, `/me`, `GET /families`, `POST /families`, `/notifications*` |

- No header, with several dependents: `400 FAMILY_REQUIRED`.
- A family that isn't yours: `404 NOT_FOUND`.
- Dependents can leave it out, since they have one family. If they do send it, it must be
  their own.

The simplest approach is to set the header on every guardian request once a dependent is
selected, and to change it when the guardian switches dependent.

## 3. Adding another dependent

Same call as the first registration, `POST /families`, with the same `register-dependent`
signature (`family:new`, unchanged). One rule is new: **use the same
`guardian_token_account`** as the guardian's existing families (take it from
`GET /families`). A different one is refused with `409`.

The response still contains a `token`; you can keep using the old one, since tokens don't
carry a family.

**Delegation.** The new dependent shares the existing delegation, so no new Phantom approve
is needed. But the money now covers more people, so offer the guardian a top-up. An SPL
`approve` **replaces** the delegated amount rather than adding to it, so approve the new
total.

## 4. What each screen means now

- **Budget (`/budget`).** `spent`, `reserved`, `weekly_cap` and `remaining` are this
  dependent's own week. `delegated_remaining` is the **shared** reservoir, after money still
  in flight for *any* dependent. Show it as shared ("Reservoir for all your dependents").
- **Policy interview.** One per dependent: start it with that dependent selected.
- **Signed messages.** The `family:{family_id}` line is the family you're acting on, and it
  must be the same family as the header:
  - `activate-policy` and `freeze`: the selected family;
  - `answer-question`: the question's family (answer it with that family selected, or you get
    `404`).
  - Freeze is per dependent; "freeze everyone" means one signature per family.
- **Questions.** Per family. For a single inbox, call `GET /questions?status=open` once per
  family and merge. The agent's "top up the reservoir" question is filed under the guardian's
  **first** dependent (first in `/families`).
- **Notifications (`/notifications`).** All of the caller's notifications, from every family,
  without the header. Each has `family_id`:
  - an id: about that dependent; label it with the dependent's name;
  - `null`: about the guardian as a whole (for example the reservoir).

  Weekly summaries and the agent's notes to the guardian currently arrive with `family_id:
  null`; the text names the dependent. We've asked the agent layer for a field so they can be
  filed per dependent; the response shape won't change.
- **Live events (`/events`).** One stream per family, chosen by the header. To get live
  updates for every dependent, open one stream per family. A guardian-wide notification is
  sent on every stream, so de-duplicate by `notification_id`.

## Checklist

- [ ] Read `family_ids` after sign-in; add a dependent picker for 2+ dependents.
- [ ] Send `X-Family-Id` on guardian calls about one dependent; handle `400 FAMILY_REQUIRED`.
- [ ] "Add dependent" flow: `POST /families` with the existing `guardian_token_account`;
      offer a delegation top-up (approve the new total).
- [ ] Label the reservoir as shared on the budget screen.
- [ ] Merge questions and notifications across families; group by `family_id` (`null` =
      everyone).
- [ ] One SSE stream per family; de-duplicate notification events.
- [ ] Dependent app: no changes.
