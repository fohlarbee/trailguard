# Integrating with TrailGuard

This guide is for a bank or fintech engineer connecting an institution to TrailGuard. It covers the sandbox, the connector, the event format, and how hold requests reach your fraud desk.

## What you install

One container, the connector, running inside your network. It:

- reads your events and hashes every identifier before anything leaves your network
- sends the hashed events to TrailGuard over an outbound HTTPS connection
- collects hold requests for your accounts and hands them to your fraud desk
- passes your answer back

It needs no write access to any of your systems and accepts no inbound connections from outside your network.

| Property | Value |
|---|---|
| Direction of traffic to TrailGuard | Outbound only |
| Data that leaves your network | Hashed account, BVN, phone and device identifiers; amounts; timestamps; channel |
| Data that never leaves | Names, raw account numbers, raw BVNs, balances |
| Local state | One SQLite file: read position, and a hash-to-account lookup for your own accounts |

The hash is a keyed HMAC-SHA256 using a salt shared by all member institutions. That shared salt is what lets a transfer from your bank be matched to the receiving bank. Because account numbers and BVNs are short, anyone holding the salt could reverse the hashes, so treat it as a secret key. This is pseudonymisation, not anonymisation.

## 1. Try the sandbox

No credentials or real data are needed.

```
docker compose -f docker-compose.sandbox.yml up --build
```

This starts a complete TrailGuard, fills it with three days of synthetic activity from six made-up institutions, and runs one connector (for `BNK01`, in shadow mode) against a sample feed. The first start takes a few minutes while it trains the scoring model.

| What | Where |
|---|---|
| API reference, interactive | http://localhost:8010/docs |
| API key for the sample bank | `sandbox-bnk01` (header `X-API-Key`) |
| Connector health | http://localhost:8088/health |

Try:

```
curl -H "X-API-Key: sandbox-bnk01" http://localhost:8010/v1/cases
curl -H "X-API-Key: sandbox-bnk01" http://localhost:8010/v1/holds
```

Stop and remove it with `docker compose -f docker-compose.sandbox.yml down -v`.

## 2. Configure the connector

Copy `connector/connector.example.toml` to `connector.toml`. Secrets come from environment variables, never from the file.

| Setting | Meaning |
|---|---|
| `mode` | `shadow` or `live` (see below) |
| `[bank] id` | The institution ID TrailGuard issued you |
| `[trailguard] url` | The TrailGuard API address |
| `[source] type` | `file`: the connector reads a file you append to. `http`: you POST events to the connector. |
| `[holds] deliver` | `file`: hold requests are appended to a file. `webhook`: they are POSTed to your URL. |
| `[local] listen` | Address of the connector's local endpoint, for your systems only |
| `[local] state` | Where the connector keeps its SQLite state file |

Run it:

```
docker build -f Dockerfile.connector -t trailguard-connector .
docker run -d --name trailguard-connector \
  -e TRAILGUARD_API_KEY=... -e TRAILGUARD_SALT=... \
  -v /path/to/connector.toml:/config/connector.toml:ro \
  -v /path/to/data:/data \
  -p 127.0.0.1:8088:8088 \
  trailguard-connector
```

### Shadow mode first

Start in `shadow` mode. The connector sends events and TrailGuard scores and traces them, but no hold request reaches your fraud desk. Each one is written to a local log (`<state file>.shadow.jsonl`) with the real account number, so you can compare what TrailGuard would have held against what your own team found. Switch to `live` when you are satisfied.

In shadow mode the hold requests still exist on TrailGuard's side and lapse unanswered after 24 hours.

## 3. Send events

Events are JSON objects, one per line in file mode or in an `events` array in HTTP mode. Send raw identifiers: the connector hashes them. Amounts are integers in kobo.

**Account opened.** Send one for every existing account as an initial load, then one whenever an account is opened.

```json
{"type": "account_opened", "account_number": "0123456789", "bank_id": "BNK01",
 "bvn": "22123456789", "phone": "08012345678", "device_id": "DEV-123",
 "kyc_tier": 1, "account_type": "personal", "opened_at": "2026-10-05T09:14:00+01:00"}
```

`account_type` is `personal`, `business` or `pos_agent`.

**Transaction.** Send every transfer and withdrawal where your institution is the sender or the receiver. For a cash withdrawal, both receiver fields are `null`.

```json
{"type": "transaction", "transaction_id": "TX123", "session_ref": "000011261005091400000000000123",
 "timestamp": "2026-10-05T09:14:00+01:00", "sender_bank": "BNK01", "sender_account": "0123456789",
 "receiver_bank": "FIN02", "receiver_account": "9876543210", "amount_kobo": 5000000,
 "channel": "mobile", "device_id": "DEV-123"}
```

`session_ref` must be the NIBSS session ID or another reference both banks share. It is how the two banks' copies of one transfer are recognised as the same transfer. `channel` is `mobile`, `web`, `ussd`, `pos` or `atm`.

**Fraud report.** Send when a customer reports a fraud. This opens a case.

```json
{"type": "fraud_report", "report_id": "RPT42", "reported_at": "2026-10-05T10:02:00+01:00",
 "reporting_bank": "BNK01", "victim_account": "0123456789", "transaction_ids": ["TX123"]}
```

Rules:

- You may only send events your institution took part in. Anything else is rejected with `422`.
- Resending is safe. Events already stored are ignored.
- In file mode the connector resumes from where it stopped after a restart or an outage.
- In HTTP mode, a `503` from the connector means TrailGuard was unreachable: keep the events and send them again.

## 4. Receive and answer hold requests

When an account at your institution is flagged, or the investigator finds stolen money in it, the connector delivers a hold request:

```json
{"hold_id": 17, "account_number": "0123456789", "amount_kobo": 14205100,
 "reason": "Flagged by the mule scorer: account opened 4 days ago; device registered to 4 other account(s)",
 "placed_at": "2026-10-05T22:33:10+01:00", "expires_at": "2026-10-06T22:33:10+01:00", "shadow": false}
```

Place the hold in your own systems, then report how much you actually held:

```
curl -X POST http://localhost:8088/holds/17/answer \
  -H "Content-Type: application/json" -d '{"held_kobo": 14205100}'
```

Answer `0` if there was nothing left to hold.

Holds are temporary by design:

- A hold lapses at `expires_at`, 24 hours after it was placed, unless a named person at your institution confirms it.
- That person can release it at any time.
- If the investigator later finds the hold covers more than the stolen amount, it recommends a reduction. Only your staff can accept it.

Confirming, releasing and reducing are done in the fraud desk dashboard or through the API (`POST /v1/holds/{id}/review`, `POST /v1/holds/{id}/reduce`). Only the institution that holds the account can do these.

## 5. Check it is working

```
curl http://localhost:8088/health
```

```json
{"bank": "BNK01", "mode": "shadow", "events_sent": 13251, "holds_delivered": 4,
 "holds_answered": 0, "last_error": null}
```

`last_error` shows the most recent failure to reach TrailGuard, and clears on the next success.

## What is not built yet

- TLS termination and key rotation are left to your own infrastructure and to the hosted service.
- Input adapters exist for a file and for HTTP only. Message queues and database change feeds are not yet supported.
- The connector does not validate events locally. An invalid event is rejected by TrailGuard and the batch is retried, so one bad record blocks the file until it is corrected.
