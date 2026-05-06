# CryptoBot

Modulaire Python trading bot architectuur voor Bitvavo met een manager/agent model, Docker support, web UI en een eerste strategie: static grid.

## Wat je nu krijgt

- 2 containers:
  - `manager`: control plane, API, web UI, opslag van bot-configs en metrics
  - `agent`: execution node die bots draait en data terugstuurt
- Agent discovery + approval:
  - agent meldt zich automatisch bij manager als `pending`
  - manager keurt agent goed via de UI
  - manager kan agent ook `rejecten` of terug op `pending` zetten (`unapprove`)
  - pas na goedkeuring kan een agent bots starten
  - de UI toont een popup bij nieuwe discovery events en bewaart notificatie-historie
  - de UI kan logs tonen van approved agents (trades, skips, start/stop, budget updates) in een modal dialog met live refresh
- Modulair strategie-framework (`common/strategy`) zodat je later extra methodes kunt toevoegen.
- Simulatie/backtest endpoint (`/api/backtest`) voor vertrouwen en tuning.
- Static grid profitability preview vóór bot-creatie in de UI (incl. winst per trade inschatting).
- UI toont voor geselecteerde market pair realtime 24h summary via Bitvavo WebSocket (last price, 24h verschil, 24h volume) met groen/rood styling.
- Market selectie in UI gebruikt nu een dropdown gevuld uit Bitvavo `/v2/markets`; base/quote velden zijn verwijderd en automatisch afgeleid uit de gekozen market.
- Budget allocatie per bot (`quote_budget` en/of `base_budget`) als gereserveerd stuk van je Bitvavo wallet.
- Live mode gebruikt nu een echte Bitvavo WebSocket implementatie (auth, ticker stream, order placement).
- Bot-evaluatie loopt websocket-gedreven op prijsupdates; er is geen handmatige tick-seconds instelling meer in de UI.
- Profit mode:
  - `compound`: winst blijft in bot-budget
  - `skim`: winst wordt deels afgeroomd (in simulatie als quote)

## Architectuur

```text
[Web UI] -> [Manager API + DB] <-> [Agent(s)] -> [Strategy + Exchange Adapter]
```

- Manager bewaart configuratie, status en laatste metrics van alle bots.
- Agent voert bot-loops uit, berekent PnL en report periodiek naar manager.
- Je kunt later meerdere `agent` containers draaien en ze vanuit 1 manager UI beheren.
- Basis fail-over is aanwezig: manager detecteert heartbeat timeout en probeert running bots over te zetten naar een andere goedgekeurde online agent.
- Exchange integratie is modulair: de runner praat tegen de `Exchange` interface; wisselen van exchange vraagt alleen een nieuwe adapter-implementatie.

## Projectstructuur

```text
.
├── manager/
│   ├── app/
│   │   ├── database.py
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── schemas.py
│   │   ├── services/
│   │   │   ├── agent_client.py
│   │   │   └── backtest.py
│   │   └── static/
│   │       └── index.html
│   ├── Dockerfile
│   └── requirements.txt
├── agent/
│   ├── app/
│   │   ├── main.py
│   │   └── runner.py
│   ├── Dockerfile
│   └── requirements.txt
├── common/
│   ├── models.py
│   ├── exchange/
│   │   ├── bitvavo.py
│   │   └── simulated.py
│   └── strategy/
│       ├── base.py
│       └── static_grid.py
├── docker-compose.yml
└── .env.example
```

## Starten

1. Maak `.env`:

```bash
cp .env.example .env
```

2. Start compose:

```bash
docker compose up --build
```

3. Open UI:

- `http://localhost:8000`

## Belangrijke endpoints

- `POST /api/bots` bot aanmaken
- `POST /api/bots/{bot_id}/start` bot starten
- `POST /api/bots/{bot_id}/stop` bot stoppen
- `POST /api/bots/{bot_id}/budget` budget aanpassen
- `GET /api/bots` alle bots + laatste metrics
- `POST /api/backtest` simulatie/backtest draaien
- `POST /api/strategy/static-grid/preview` winstgevendheid + winst per trade voor static grid
- `GET /api/agents` agents + approval status
- `POST /api/agents/{agent_id}/approve` agent goedkeuren
- `POST /api/agents/{agent_id}/reject` agent afwijzen
- `POST /api/agents/{agent_id}/unapprove` agent terug op pending zetten
- `GET /api/agent-events` agent discovery/approval eventfeed
- `GET /api/agents/{agent_id}/logs` logs ophalen van approved agent

Logcategorieen die nu gebruikt worden:

- `system`: startup, registratie, heartbeat status, bot lifecycle, budget updates
- `trading`: trade uitvoeringen en overgeslagen trades

## Live Bitvavo integratie (volgende fase)

Bitvavo WebSocket live integratie staat nu in `common/exchange/bitvavo.py`.

Benodigde env vars voor live mode in agent:

- `LIVE_EXCHANGE_PROVIDER=bitvavo`
- `BITVAVO_API_KEY=...`
- `BITVAVO_API_SECRET=...`

Voor extra hardening kun je hierna toevoegen:

- reconnect met exponential backoff
- order lifecycle/fill tracking (open, partial, filled)
- precieze fee accounting per trade
- strengere order guards (min notional, max slippage)
- risico-controls (max open orders, slippage guards)

## Uitbreiden met nieuwe strategie

1. Voeg nieuwe class toe in `common/strategy/`, gebaseerd op `Strategy` uit `base.py`.
2. Maak in de agent een strategy factory op basis van `config.strategy`.
3. Voeg UI velden toe voor de nieuwe strategy-parameters.

## Opmerkingen over budget/wallet model

- `quote_budget` en `base_budget` representeren het gereserveerde bot-deel van je exchange wallet.
- Manager bewaart die allocatie in bot-config.
- In simulatiemodus wordt met virtuele balances gewerkt.
- In live modus moet je allocatie valideren tegen echte Bitvavo wallet balances.

## Security checklist (voor productie)

- Secrets in Docker secrets of vault (niet in plaintext `.env` in productie)
- API auth tussen manager en agents
- TLS/HTTPS en netwerksegmentatie
- audit logging voor start/stop/config wijzigingen

