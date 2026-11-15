# TrailGuard Architecture

Draft 1 · 15 November 2026

TrailGuard follows stolen money across banks in real time and helps freeze it before cash-out. This document describes what we build, in what order, and why each choice was made. It is written for a solo builder, so the rule throughout is: the fewest moving parts that still make the product real.

## 1. Product form

TrailGuard is a hosted service, run in-country, with three surfaces. It is not a mobile app.

| Surface | Who uses it | What it is |
|---|---|---|
| Connector | Banks | A Docker container the bank runs inside its own network |
| API and webhooks | Fintechs and PSPs | Push transaction events in, receive hold alerts back |
| Web dashboard | Fraud desk analysts | Cases, fund trails, hold approvals, evidence packs |

### The connector

This is the "install once" piece. It:

- reads transaction events from the bank (adapters for a message queue, a webhook, or a database change feed)
- hashes BVN, phone and device IDs locally with a salt, before anything leaves the bank
- sends only hashed IDs, amounts, timestamps and channel to TrailGuard
- receives hold alerts for accounts at that bank and forwards them to the fraud desk

Its connections are outbound-only and its access to bank systems is read-only. Those two properties are what a bank's security team checks first.

### What "no hassle" realistically means

No bank integration is hassle-free: security review and procurement always happen. The design shortens them with:

- **Sandbox:** the connector ships with synthetic data, so a bank can see it working before connecting anything real.
- **Shadow mode:** TrailGuard scores and traces on live data but issues no holds, so the bank can judge accuracy at zero risk.
- **One config file:** bank ID, input adapter, salt, and endpoint.

## 2. Stack

Two languages and one database.

| Layer | Choice | Why |
|---|---|---|
| Backend, ML, agent, simulator, connector | Python (FastAPI) | One language across everything that touches data |
| Dashboard | TypeScript (Next.js) | Standard, fast to build, easy to hire for later |
| Storage | Postgres | Transactions, account graph, cases and audit log in one place |
| Agent model | Claude API with tool use | Strong tool calling and multilingual output |
| Packaging | Docker | The form banks already accept |

### Left out of the first build

| Item from the deck | Decision | Reason |
|---|---|---|
| Blockchain | Replace with a hash-chained log in Postgres | Same tamper-evidence, far less to build and defend. A permissioned ledger can be added later if a regulator asks. |
| Neo4j | Defer | Postgres handles hop-by-hop tracing at pilot scale |
| Kafka | Defer | Add when a real bank's volume demands a streaming layer |
| Graph neural networks | Defer | They need real data to beat a simpler model |
| Federated learning | Defer | Only meaningful once several banks are live |

The deck's technology slide should be updated to match before the next pitch.

## 3. System overview

```
 Bank / fintech                       TrailGuard (hosted in-country)
┌──────────────────┐         ┌───────────────────────────────────────────┐
│ Core banking     │         │  Ingest API                               │
│   │              │         │     │                                     │
│   ▼              │ hashed  │     ▼                                     │
│ Connector ───────┼────────►│  Postgres ◄──► Fund-trail tracer          │
│   ▲              │ events  │     ▲     ◄──► Mule scorer                │
│   │ hold alerts  │         │     │                                     │
│   └──────────────┼◄────────┤  Case and hold service ◄──► Agent         │
│ Fraud desk       │         │     │                        │            │
└──────────────────┘         │     ▼                        ▼            │
                             │  Evidence ledger         Dashboard        │
                             └───────────────────────────────────────────┘
```

The synthetic data simulator sits where a bank sits and talks through the same connector. Moving from synthetic to real data is a configuration change, not a rebuild.

## 4. Components

### 4.1 Synthetic data simulator

Generates the world we test against.

- **Entities:** several banks, customers, accounts, devices, and POS agents.
- **Normal activity:** salaries, transfers, bill payments and POS withdrawals, with volumes and channel mix calibrated to public NIBSS figures.
- **Fraud scenarios, injected with ground-truth labels:**
  - social engineering, then fan-out to several mule accounts
  - multi-hop layering across banks within minutes
  - cash-out through POS agents, ATMs or a fintech wallet
  - insider-assisted transfers
  - legitimate look-alikes (a business paying many suppliers at once), so false positives can be measured
- **Output:** an event stream in the same format the connector expects.

Every fraud scenario records which accounts are mules and which transactions are tainted. That is what lets us put real numbers on accuracy.

### 4.2 Ingest and data model

Core tables:

| Table | Holds |
|---|---|
| `accounts` | Hashed account ID, bank, opening date, risk tier |
| `transactions` | Sender, receiver, amount, time, channel, bank pair |
| `cases` | A reported fraud, its status, and its 30-minute clock |
| `trail_hops` | Each hop of a traced trail, with the tainted amount |
| `holds` | Hold requests, who approved them, and each bank's response |
| `audit_log` | Hash-chained record of every event and decision |

### 4.3 Fund-trail tracer

Deterministic code, not AI. Given a reported transaction, it follows outflows from the receiving account, hop by hop, within a time window, and tracks how much of the stolen amount reached each account. It stops at cash-out points and at accounts whose outflows are unrelated.

This must be deterministic because its output goes to court.

### 4.4 Mule scorer

A gradient-boosted model that scores each account from features such as:

- account age and time since first inflow
- pass-through speed (how quickly money in becomes money out)
- fan-in and fan-out counts
- odd-hour activity
- share of counterparties that are themselves high-risk

Output is a risk tier, not a raw score. Accuracy is measured against the simulator's labels.

### 4.5 Case and hold service

- opens a case when a fraud report arrives and starts the 30-minute clock
- sends hold alerts to downstream banks through their connectors or webhooks
- tracks acknowledgements, rejections and amounts actually held
- places temporary holds that lapse after 24 hours unless a named person confirms them

### 4.6 Evidence ledger

Each audit entry stores the hash of the previous entry, so any later edit breaks the chain. The latest hash is published periodically to partner banks, so TrailGuard itself cannot quietly rewrite history. The evidence pack for a case is an export of its entries plus the trail and the hold record.

### 4.7 Dashboard

- live case list with the response clock
- trail view: the graph of hops, amounts and risk tiers
- hold approval queue
- evidence pack and report downloads
- a chat panel to ask the agent about a case

## 5. The agent

The agent is a case investigator that runs each case end to end by calling tools. It does not do the tracing or scoring itself; the tracer and scorer do. The agent decides what to run next, acts on the result, and explains it.

### What it does when a fraud report arrives

1. Pulls the reported transaction and runs the trace.
2. Scores every account on the trail and identifies the ring.
3. Places temporary holds on accounts still holding stolen money, with amount and reason.
4. Leaves each hold for a person at the bank to confirm or release.
5. Chases banks that have not acknowledged.
6. Re-traces if the money moves again, and updates the holds.
7. Builds the evidence pack and drafts the suspicious transaction report.
8. Answers analyst questions about the case in plain language.

Later: takes victim reports over WhatsApp and USSD in English, Pidgin, Yoruba, Hausa and Igbo.

### Tools

| Tool | Does | Needs human approval |
|---|---|---|
| `get_case` | Read the case and its reported transactions | No |
| `trace_funds` | Run the tracer from a transaction | No |
| `score_accounts` | Get risk tiers for accounts on a trail | No |
| `find_ring` | Find accounts linked by shared devices or counterparties | No |
| `place_hold` | Place a temporary hold, limited to traced stolen money | Reviewed afterwards |
| `list_holds` | See which banks have responded | No |
| `remind_bank` | Chase a bank that has not acknowledged | No |
| `build_evidence_pack` | Assemble the court-ready pack | No |
| `draft_str` | Draft the suspicious transaction report | No |
| Filing the report | Not a tool: only a human can file | **Yes** |

### Fixed rules

- **Holds are temporary, and a human reviews every one.** A flagged account, or an account the agent finds holding stolen money, is held automatically for 24 hours. A person at the account's bank then confirms or releases it; if nobody does, it lapses. Waiting for approval first does not work: in the simulator, mules move money on about five minutes after receiving it. An automatic hold asks for the money the account has just received, and the bank holds at most the available balance. The agent can only hold money the trace shows is stolen and still in the account, and that limit is enforced in code. Once a case shows how much in an account is actually stolen, the agent can recommend reducing a larger hold to that amount; only a person at the bank can accept the reduction, and it can never go below the traced stolen amount. Freezing an innocent person's money remains the costliest mistake this system can make. (Decided 15 November 2026; the legal basis is still an open question, see section 8.)
- **The agent sees only hashed IDs and risk tiers.** The model runs outside Nigeria, and the product promises data localisation. No raw customer data goes to the model.
- **Every tool call is written to the audit log,** including the agent's stated reason.
- **Numbers come from tools, never from the model.** Amounts, hops and scores in any report are copied from tool output.

## 6. Build order

| Step | Deliverable | Done when |
|---|---|---|
| 1 | Simulator with labelled fraud scenarios | It produces a multi-bank event stream with ground truth |
| 2 | Ingest, schema and tracer | A reported theft yields the correct trail on simulator data |
| 3 | Mule scorer | Precision and recall are measured against ground truth |
| 4 | Agent with tools and the approval step | It runs a case from report to evidence pack |
| 5 | Dashboard | An analyst can watch a case and approve holds |
| 6 | Connector, sandbox and integration docs | A third party can run the sandbox from the docs alone |

Steps 1 to 4 together are the live demo the pitch deck promises.

## 7. Repository layout

```
trailguard/
├── simulator/      synthetic banks, customers and fraud scenarios
├── connector/      the container banks install
├── core/           ingest API, schema, tracer, scorer, case and hold service, ledger
├── agent/          tools, prompts and the case loop
├── dashboard/      Next.js app
├── docs/           integration guide and API reference
└── pitch-deck/     deck source and PDF
```

## 8. Open questions

These are not engineering problems, and they decide whether the product can go live.

- **Legal basis for holds.** Under what CBN rule can a bank hold funds on a third party's alert, and for how long, before a court order is needed?
- **Data access.** Without NIBSS or most large banks, the trail has gaps. Which route is realistic first: one bank plus partner fintechs, or NIBSS directly?
- **Data protection.** Whether salted, hashed BVNs count as personal data under the Nigeria Data Protection Act, and what that requires of us.
- **Liability.** Who carries the cost when a hold turns out to be wrong?
