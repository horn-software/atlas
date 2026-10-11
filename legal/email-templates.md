# Atlas Email Templates

These are the transactional email templates to set up in Resend.
Each section shows the subject line, plain-text body, and notes.

---

## Template 1: Licence Key Delivery

**Subject:** Your Atlas licence key

**Body:**
```
Hi there,

Thanks for subscribing to Atlas [TIER]!

Here is your licence key:

  [LICENCE_KEY]

To activate Atlas, run:

  atlas activate [LICENCE_KEY]

That's it — your codebase querying upgrades are unlocked immediately.

---

What's next:
1. Run `atlas ingest --project my-project` to index your codebase
2. Open your AI coding assistant and try: "What functions handle authentication?"
3. Check the docs at hornsa.com/atlas/docs

Questions? Reply to this email or join the community at github.com/horn-software/atlas/discussions.

—
The Atlas team
hornsa.com/atlas
```

**Template variables:** [TIER], [LICENCE_KEY]
**Send trigger:** `order_created` Lemon Squeezy webhook — fires from `_handle_order_created` in `license_server/main.py`

---

## Template 2: Trial Expiry Warning (Day 6 of 7-day trial, if trial is added later)

**Subject:** Your Atlas Pro trial ends tomorrow

**Body:**
```
Hi there,

Your Atlas Pro trial ends tomorrow.

If you've been enjoying faster context loading and multi-language support,
you can keep access by subscribing at hornsa.com/atlas.

If you choose not to subscribe, Atlas will revert to the Free tier:
- 1 project only
- Python only
- No trained model (raw keyword search)

Your indexed data stays on your machine and doesn't disappear.

Subscribe → hornsa.com/atlas

—
The Atlas team
```

**Template variables:** none
**Send trigger:** Scheduled job on day 6 of trial (future feature)

---

## Template 3: Subscription Cancelled / Key Downgraded

**Subject:** Your Atlas Pro subscription has ended

**Body:**
```
Hi there,

Your Atlas Pro subscription has ended and your licence key has been downgraded to Free tier.

What this means:
- Multi-project support is now limited to 1 project
- The trained query model and verifier are disabled
- Raw keyword search (the open-source core) still works

You can resubscribe at any time at hornsa.com/atlas and your projects will be available again immediately.

Resubscribe → hornsa.com/atlas

If you cancelled by mistake or have questions, reply to this email.

—
The Atlas team
```

**Template variables:** none
**Send trigger:** `subscription_cancelled` or `subscription_expired` Lemon Squeezy webhook

---

## Template 4: Payment Failed

**Subject:** Atlas — action required: payment failed

**Body:**
```
Hi there,

We were unable to process your Atlas subscription payment.

To keep your Pro features active, please update your payment method in the customer portal:

  [CUSTOMER_PORTAL_URL]

If we cannot collect payment within a few days, your subscription will be cancelled
and your licence key will be downgraded to the Free tier.

If you believe this is an error, contact hello@hornsoftwarearchitects.co.za.

—
The Atlas team
```

**Template variables:** [CUSTOMER_PORTAL_URL]
**Send trigger:** `subscription_payment_failed` Lemon Squeezy webhook (only when status is `past_due`)

---

## Resend Setup Notes

1. Create a Resend account at resend.com
2. Add and verify the domain `hornsa.com`
3. Create each template above in Resend → Templates
4. In `license_server/main.py`, replace `_log_pending_key()` with a call to the Resend API:

```python
import resend
resend.api_key = os.environ.get("RESEND_API_KEY", "")

def _send_key_email(email: str, tier: str, key: str):
    resend.Emails.send({
        "from": "Atlas <hello@hornsoftwarearchitects.co.za>",
        "to": [email],
        "subject": "Your Atlas licence key",
        "html": _render_key_email(tier, key),
    })
```

5. Add `RESEND_API_KEY=re_xxxx` to `license_server/.env`
6. Add `resend>=2.0.0` to `license_server/requirements.txt`
