# TraceOps

When one part of an app breaks, every part that depends on it starts showing errors too.
TraceOps figures out **which part actually broke**.

It comes with a small demo shop you can break on purpose, and a dashboard that shows
what TraceOps found.

**Built with:** Python, FastAPI, PostgreSQL, React, TypeScript, Docker, OpenTelemetry,
Prometheus, Grafana Loki, Grafana Tempo

## The idea

The demo shop has four parts in a line. Every customer request passes through all of them:

```
Customer → Gateway → Orders → Inventory → Database
```

If the database stops working, Inventory fails, so Orders fails, so the Gateway fails.
You get errors from **every** part, but only one of them is the real cause.

TraceOps finds it like this:

1. **Each service records what it does:** request counts and timings (metrics), error
   messages (logs), and the path each request took through the services (traces).
2. **An alert fires** when too many requests fail or get slow.
3. **TraceOps looks at failed requests step by step.** The first step that failed is where
   the problem started. It checks that the error logs and the numbers agree.
4. **It saves a report:** what broke, how sure it is, and the evidence.

## Run it

You need **Docker Desktop** and **Node.js**.

```bash
git clone https://github.com/sanchayi21/TraceOps.git
cd TraceOps
cp .env.example .env
docker compose up -d --build
```

Then start the dashboard:

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**.

To stop everything: `docker compose down`.

## Try it

On the dashboard:

1. Click **Start traffic**. This sends pretend customers to the shop.
2. Wait 30 seconds, then pick something to break, e.g. **Database goes offline**.
3. Wait about a minute. An incident appears. Click it to see what TraceOps found.
4. Click **Fix it**.

TraceOps is never told what you broke. It works it out from the evidence.

| What you break | What TraceOps should say |
|---|---|
| Database goes offline | The database stopped accepting connections |
| Database gets slow | Database queries became very slow |
| Database gets very slow (requests time out) | Database queries became very slow. **Not** Orders, even though Orders is where the timeout error shows up |
| `docker compose stop inventory` | The Inventory service is down |

There's also a script that runs a scenario and prints the result:
`python scripts/demo.py db-down` (or `slow-db`, `timeout`, `inventory-down`).

## How sure is it?

TraceOps gives each finding a simple score:

- Most of the score comes from **how many failed requests broke at the same place**.
- The rest comes from whether **the error logs agree** and whether **the numbers agree**.

It's a rule of thumb, not a precise probability, and the report says what it isn't sure about.

## Bugs I found while building it

1. **The app blamed the wrong service.** Orders and Inventory both waited 3 seconds before
   giving up. When the database broke, Orders gave up first and reported "can't reach
   Inventory", even though Inventory was fine. Fixed by making inner services give up sooner.
2. **Logs and traces didn't match up.** Tempo shortens some trace IDs by dropping the leading
   zero, so about 1 in 16 traces couldn't be matched to its logs. Fixed by padding IDs back.
3. **"Can't connect" was reported as "too slow".** In the httpx library, a connection timeout
   counts as a kind of timeout, so a stopped service looked slow instead of down.
4. **Healthy services looked down.** When Inventory was stopped, other services got stuck
   waiting on it, and Prometheus couldn't read their metrics either. Fixed by making the
   metrics endpoint async.

## Tests

```bash
cd backend && python -m pytest
python -m pytest services/tests
```

The tests use fake traces, so they run without Docker.

## Project layout

```
backend/     TraceOps: API, investigator, incident storage
services/    the demo shop: gateway, orders, inventory
frontend/    React dashboard
infra/       config for Prometheus, Loki, Tempo, Alertmanager, Grafana
scripts/     demo scenario script
```


## Limitations

- Runs on one machine, for demos. Not built for a large production system.
- Looks at up to 40 failed requests per investigation.
- No login. Everything only listens on your own computer (`127.0.0.1`).
