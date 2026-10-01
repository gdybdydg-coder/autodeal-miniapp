# Receiving-details preview: live verification — 2026-10-01

Release main is `017358aedd39a1903c63c729811715f86ed2f490`, tree
`76018dc903a87e6a5624053f80ce344a4d57eafe`. All 145 remote file entries match
the tested local snapshot. The local Git root tree was recomputed independently
and matches exactly. Main advanced without force; other WIP code was not merged.
Full offline regression: **1029 passed in 146.18 seconds**; focused: **84 passed
in 4.14 seconds**. No paid development API call or Telegram test message was sent.

One server-only `SUBSCRIPTION_PREVIEW_RECIPIENT_JSON` key was merged with
`replace: false`. The user's approved profile has `mode: test_only`; its IBAN
structure/checksum passed the same code validation locally. Values are not in
the repository, this record or public source-status. Existing secret values
were neither requested nor printed. Bank collection/paywalls remain disabled.

## Actual deployment behavior

Unlike a raw API environment save, the connected environment-update tool also
initiated an API deployment of the then-current old main:

- `dep-davc0p3bc2fs73ca3br0`, old commit `71bbf246870479362f7fbc48f2ba1afd29a8edd3`,
  finished live at **2026-10-01T20:20:54.894062Z**.

This was detected in deployment history before any manual deployment. The old
deployment was allowed to finish; no second deployment was queued concurrently.
The new release's `[skip render]` prevented a separate automatic code deploy.
The one manual deployment of the tested release is:

- **`dep-davc1orbc2fs73ca6rg0`**, commit
  **`017358aedd39a1903c63c729811715f86ed2f490`**, live at
  **2026-10-01T20:22:51.486632Z** (**23:22 Kyiv**).

The final history has exactly one deployment of this candidate commit; it is the
current live service. No resource, plan, auto-deploy setting or existing quota
configuration was changed. This follow-up is saved only on the WIP branch and
does not trigger another production deployment.

For future combined private-config releases, move the tested code with a skip
phrase first, then merge the new configuration so the connector's own deployment
can target the correct current main. Always inspect actual deploy history and
reuse an existing matching deployment; do not assume this connector is save-only
based on the lower-level REST documentation.

## Post-release read-only checks

- Health succeeded: PostgreSQL connected, Telegram delivery available.
- Application error logs since the target deployment started were empty.
- Initial projected source-status reads timed out (including after deployment).
  A later bounded retry succeeded; no full source-status was printed. The
  endpoint reads cached diagnostics and never spends paid API requests.
- Monitor running, status idle, **76** active groups / **76** successful groups.
- Night interval **3600 seconds**; cursor lag **1697 seconds**,
  `needs_attention=false` under the active night schedule.
- Valuation queue **0**, pending monitor jobs **0**, Telegram delivery queue **0**.
- `confirmed_deals_only=true`; active-window and include-initial both false.
- Quota available. Limits still **4500/hour**, **90000/day**, **1102160 lifetime**.
  Local lifetime used **268168**, local remaining **833992**. These are our ledger
  values, not the provider account balance; nothing was reset.

These checks show a healthy service and working monitoring state. They do not
prove complete listing coverage, delivery push on the owner's phone, a real
payment, customer entitlement or Telegram client's native-copy rendering.

## Owner check and next decision

Open `/subtest` → **🏦 Перегляд реквізитів**. Both the receiving card and
**✅ Я оплатив · тест** are owner-only protected previews. Use only the explicit
synthetic receipt/approval controls in the existing trial; actual photos/PDFs do
not count as bank reconciliation. `/subtest_admin` remains the owner's panel.

A one-time reminder for **2026-10-02 12:00 Europe/Kyiv** was created successfully.
It requests a private test and expressly forbids automatic payment/config/access
activation. The public bot is still free. Digital access sold inside Telegram
requires a Stars-compatible purchase flow, its agreed price and a separate launch
decision; this IBAN illustration cannot be made real billing by toggling a flag.

Do not restore stopped searches, reset epochs/claims/accounting, repeat recovery,
change discovery policies or retrieve server secrets during the owner check.
