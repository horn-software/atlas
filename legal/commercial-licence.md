# Atlas Commercial Licence Agreement

**Last updated: 11 October 2026**

This Commercial Licence Agreement ("Agreement") governs your use of the proprietary components of Atlas. By activating a paid Atlas subscription, you accept this Agreement.

---

## 1. Parties

**Licensor:** Petrus Horn trading as Horn Software Architects, South Africa ("we", "us")
**Licensee:** The individual or entity that purchased the subscription ("you")

---

## 2. Proprietary Components

The following Atlas components are proprietary and are licensed, not sold, under this Agreement:

- The trained BiLSTM query model weights (`query_model.pt`, `vocab.json`, `config.json`)
- The training pipeline (`train.py`, `generate_data.py`)
- The verifier bridge (`bridge.py`)
- The licence key generator (`keygen.py`)
- The licence server software and infrastructure

The open-core shell (ingest pipeline, MCP server, call graph walker, model architecture definition, CLI) is separately licensed under the Apache 2.0 licence and is NOT subject to this Agreement.

---

## 3. Licence Grant

Subject to your active paid subscription and compliance with this Agreement, we grant you a:

- **Personal, non-exclusive, non-transferable, non-sublicensable** licence to
- **Install and use** the proprietary components
- **On machines you own or control**
- **For your own internal development and commercial work**

### Tier limits
| Tier | Machines / Seats |
|------|-----------------|
| Pro | 1 user, up to 3 machines |
| Team | Number of purchased seats |
| Enterprise | As agreed in writing |

---

## 4. Restrictions

You must not:

1. **Redistribute** the proprietary model weights or any portion of them
2. **Reverse-engineer** the weights, training pipeline, or verifier bridge
3. **Extract** training data or attempt to reproduce the training process from the weights
4. **Sub-license** the proprietary components to any third party
5. **Share** your licence key beyond your purchased seat count
6. **Build a competing product** that replicates Atlas's core functionality using knowledge derived from the proprietary components and offers it to third parties
7. **Circumvent** the licence enforcement mechanism
8. **Use** the proprietary components after your subscription has expired or been cancelled

---

## 5. Delivery

Proprietary components are delivered via secure download from our licence server (`/v1/weights/`) authenticated by your licence key. You are responsible for keeping your licence key confidential.

---

## 6. Updates

While your subscription is active, you are entitled to all updates to the proprietary components released during your subscription period. Updates do not reset the subscription term.

---

## 7. Ownership

All intellectual property rights in the proprietary components remain exclusively with Petrus Horn trading as Horn Software Architects. Nothing in this Agreement transfers ownership.

---

## 8. Term and Termination

This Agreement is effective from the date of your first purchase and continues for as long as your subscription remains active.

This licence terminates automatically and immediately if:
- Your subscription expires, is cancelled, or is refunded
- You materially breach any provision of this Agreement and fail to cure within 14 days of written notice
- You become insolvent or enter into bankruptcy proceedings

On termination, you must delete all copies of the proprietary components from your machines.

---

## 9. Warranty Disclaimer

The proprietary components are provided "AS IS" without warranty of any kind, express or implied. We do not warrant that they will be error-free, accurate, or fit for any particular purpose.

---

## 10. Limitation of Liability

To the maximum extent permitted by South African law, our total liability for any claim under this Agreement is limited to the subscription fees you paid us in the 12 months preceding the claim.

We are not liable for indirect, incidental, consequential, or punitive damages, or for loss of profits, data, or business opportunities.

---

## 11. Governing Law

This Agreement is governed by the laws of the Republic of South Africa. Any disputes shall be resolved in the courts of South Africa.

---

## 12. Entire Agreement

This Agreement, together with the Terms of Service and Privacy Policy, constitutes the entire agreement between the parties regarding the proprietary components and supersedes all prior agreements.

---

## 13. Contact

Petrus Horn trading as Horn Software Architects
Email: hello@hornsoftwarearchitects.co.za
