# Atlas Terms of Service

**Last updated: 11 October 2026**
**Effective date: 11 October 2026**

These Terms of Service ("Terms") govern your use of Atlas, a software product provided by Petrus Horn trading as Horn Software Architects ("we", "us", "our"), located in South Africa. By installing, activating, or using Atlas, you agree to these Terms.

---

## 1. The Product

Atlas is a local MCP (Model Context Protocol) server that indexes your codebase and exposes tools to AI coding assistants. It runs on your machine. Your source code is never transmitted to our servers.

---

## 2. Licence Tiers

### 2.1 Free Tier
The Free tier is provided at no cost, indefinitely. It includes the open-core shell (codebase ingestion, raw keyword search, impact analysis) limited to one project. No credit card is required.

### 2.2 Paid Tiers (Pro, Team, Enterprise)
Paid tiers are licensed on a subscription basis. Your subscription gives you:
- A licence key that unlocks the trained model, verifier bridge, and additional features
- A daily session token issued by our licence server for as long as your subscription is active
- The right to download and use the trained model weights on your machine

Paid features are delivered via the licence key. The key encodes your tier and expiry and is verified locally using a public key baked into the software.

---

## 3. What You May and May Not Do

### You may:
- Use Atlas on any number of machines you personally own or control (Pro: 1 user; Team/Enterprise: up to the purchased seat count)
- Use Atlas for commercial work, client projects, and open-source development
- Contribute to the open-source shell at github.com/horn-software/atlas

### You may not:
- Resell, sublicence, or redistribute your Atlas licence key
- Reverse-engineer, decompile, or attempt to extract the proprietary model weights or verifier logic
- Use Atlas to build a competing product that replicates its core functionality and is offered to third parties
- Share your licence key with users outside your purchased seat count
- Circumvent the licence enforcement mechanism

---

## 4. Subscription, Billing, and Cancellation

Subscriptions are billed monthly or annually through Lemon Squeezy, our payment processor. Lemon Squeezy acts as the merchant of record and handles all payment processing, tax collection, and receipts.

You may cancel at any time from the customer portal linked in your dashboard. On cancellation:
- Your subscription remains active until the end of the current billing period
- At the end of the billing period, your licence key is downgraded to Free tier
- Your model weights and session token stop working; the open-core shell continues to work

We reserve the right to change pricing. Existing subscribers are notified at least 30 days before any price increase and may cancel before the new price takes effect.

---

## 5. Refunds

See our separate Refund Policy. In summary: we offer a 7-day refund on first-time Pro subscriptions. No refunds are issued after 7 days or on renewal charges.

---

## 6. Intellectual Property

The open-core shell (ingest pipeline, MCP server, call graph walker, model architecture) is licenced under the Apache 2.0 licence. The full text is at github.com/horn-software/atlas/blob/main/LICENSE.

The training pipeline, trained model weights, and verifier bridge are proprietary and owned exclusively by Petrus Horn trading as Horn Software Architects. No rights to these components are transferred by your subscription.

---

## 7. Data and Privacy

Atlas does not transmit your source code. The only data sent to our servers is:
- Your licence key (for daily token refresh and tier verification)
- Your project ID (for free-tier project count enforcement)
- Anonymous usage counts (tool call counts, no code content)

For full details see our Privacy Policy.

---

## 8. Warranty Disclaimer

Atlas is provided "as is" without warranty of any kind. We do not warrant that Atlas will be error-free, uninterrupted, or suitable for any particular purpose. We are not liable for any loss of data, lost profits, or indirect damages arising from your use of Atlas.

---

## 9. Limitation of Liability

To the maximum extent permitted by South African law, our total liability to you for any claim arising from these Terms or your use of Atlas is limited to the amount you paid us in the 12 months preceding the claim, or R100, whichever is greater.

---

## 10. Governing Law

These Terms are governed by the laws of the Republic of South Africa. Any disputes shall be resolved in the courts of South Africa.

---

## 11. Changes to These Terms

We may update these Terms from time to time. We will notify you by email and by posting the updated Terms at hornsoftwarearchitects.co.za/atlas/legal/terms. Continued use of Atlas after the effective date of any changes constitutes acceptance of the updated Terms.

---

## 12. Contact

Petrus Horn trading as Horn Software Architects
Email: hello@hornsoftwarearchitects.co.za
Website: hornsoftwarearchitects.co.za/atlas
