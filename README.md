# TrailGuard

Follow stolen money across banks in real time, and hold it before it is cashed out.

| Folder | What it is |
|---|---|
| `simulator/` | Synthetic banks, customers and fraud scenarios with ground truth |
| `connector/` | The container a bank installs; hashes identifiers and forwards events and holds |
| `core/` | Ingest API, fund-trail tracer, live scoring, holds, evidence ledger |
| `scorer/` | Mule-account model: features, training, evaluation |
| `agent/` | The case investigator (Claude with tools) |
| `dashboard/` | Fraud desk web app (Next.js) |
| `docs/integration.md` | Guide for a bank's engineers |
| `ARCHITECTURE.md` | Design and decisions |

## Run it locally

Needs Docker, [uv](https://docs.astral.sh/uv/) and Node 20+. Copy `.env.example` to `.env` first.

```
docker compose up -d                                        # Postgres on port 5433
uv run python -m simulator generate --seed 7                # synthetic data in data/run-7
for s in 101 102 103 104; do uv run python -m simulator generate --seed $s --out data/train-$s; done
uv run python -m scorer train --train data/train-101 data/train-102 data/train-103 --validate data/train-104
uv run --env-file .env uvicorn core.api:app --port 8000     # the API, in its own terminal
uv run --env-file .env python -m core reset
uv run --env-file .env python -m connector push data/run-7 --all
cd dashboard && npm install && npm run dev                  # http://localhost:3100
```

The investigator needs `ANTHROPIC_API_KEY` in `.env`.

## Live demo

With the API and dashboard running and the scorer trained (the first five commands above), open
http://localhost:3100/demo and press **Start the demo**. It empties the database and plays one theft in
about two minutes:

1. A customer is scammed out of ₦776,660, and the money goes through a recruited customer's account
   into mule accounts at three institutions.
2. The scorer flags the mules, and the banks freeze the money and refuse to let it leave.
3. The victim reports the theft, and the AI investigator works the case at real speed.
4. The holds wait for the fraud desks to confirm them.

The clock runs at 10× while money moves, 40× through quiet stretches, and real time while the investigator
works. The theft is simulator seed 8 (`SEED` in `core/demo.py`). Without an Anthropic key the demo still
runs, and the investigator step reports that it could not start.

## Test

```
uv run pytest
```

## Sandbox

A self-contained copy with synthetic data, for trying the integration:

```
docker compose -f docker-compose.sandbox.yml up --build
```

See `docs/integration.md`.
