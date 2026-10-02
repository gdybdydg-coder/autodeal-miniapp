# Receipt collection release, 2026-10-02

## Previous production version and rollback

Before this release, main and Render ran commit `c9fe808ba4a555f2b636c4832541c0ae011392fa`, deploy `dep-davopqnavr4c73crv42g`. The exact commit is retained at `rollback/before-receipt-flow-20261002`.

The migration only creates four tables: `manual_receipt_expectations`, `manual_receipt_submissions`, `manual_receipt_notices`, and `manual_owner_confirmations`. Existing request, evidence, entitlement, audit and notice tables are not rewritten or dropped. Latest media references continue to populate existing request fields. No data restore is part of a code rollback.

The safe first response to a checkout incident is to set `MANUAL_PAYMENT_PUBLIC_ENABLED=false`, keeping review, receipt processing and transactional notices enabled. This stops new purchases while existing requests and paid access remain available. Do not reset or resume completed advertising campaigns. If a transport defect requires pausing sends, `MANUAL_PAYMENT_NOTICES_ENABLED=false` preserves the durable outbox for a reviewed restart; it temporarily delays transactional messages too.

For a frontend-only defect, restore `payment-review.html`, `payment-review.js`, and `payment-review.css` from the retained baseline with a new asset version, while retaining this backend. The legacy owner form is still accepted by this backend. Publish as a new commit; do not force-reset main.

A complete code rollback can redeploy the retained baseline without reverting the database. However, that version cannot consume the new captionless receipt waiting state or new field-free confirmation tokens. Therefore, after any new receipt flow has started, do not blindly resume checkout on the old backend. Keep new purchases paused and retain/fix this receipt adapter, or reconcile outstanding requests first. Existing evidence and approved access must remain intact. The two-step confirmation can be restarted safely from the current request card.

## Customer flow

The paid button durably records receipt expectation. A private-chat photo or JPEG/PNG/WebP document is associated with the selected request, saved as evidence, and moved to review. The highest-resolution Telegram photo is used. Duplicate updates/messages cannot add duplicate evidence; corrected images stay on the same request. Text asks for an image again, while commands continue to work. Screenshots never activate access.

The owner media notification is an outbox item committed with the evidence. Known rejections support controlled retries; unknown acceptance is not blindly resent. The persistent owner queue and authenticated receipt endpoint remain available independently of Telegram notification delivery.

## Approval and advertising

The owner explicitly verifies the bank receipt and confirms a revision-bound preview. No receiving-account, operation-number or actual-amount input is needed. Existing legacy approval requests remain compatible. Entitlement activation is atomic, idempotent and adds 30 days after the later of current expiry and now.

All three first-purchase campaign executors use the shared persisted eligibility rule at audience build and immediately before transport, including retries. The final check and bounded transport hold the same subscription-control lock used by grants, so an approval cannot commit between that check and dispatch. A single advertising send can delay a grant for the duration of one Telegram request configured with timeout=5; the HTTP adapter uses per-phase timeouts, not a strict five-second total deadline. Current and former paid users, granted access, pending payment/receipt review, stopped, opted-out and blocked users are excluded. Read failures defer advertising. Transactional and vehicle messages are outside this filter. This release does not create, resume or send a campaign.

## Verification boundaries

Synthetic tests use isolated SQLite/PostgreSQL and mocked Telegram/file transport. The deployment check must confirm the release SHA, startup receipt-route capabilities, aggregate advertising queue audit, and published admin assets. It must not send fake customer receipts or approve a real customer's payment. Actual receipt visibility on a real device and actual owner bank verification require normal owner/customer use; synthetic success is not proof a person has read a Telegram message.
