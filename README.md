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
- Modulair strategie-framework (`common/strategy`) zodat je later extra methodes kunt toevoegen.
- Simulatie/backtest endpoint (`/api/backtest`) voor vertrouwen en tuning.
- Budget allocatie per bot (`quote_budget` en/of `base_budget`) als gereserveerd stuk van je Bitvavo wallet.
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
- `GET /api/agents` agents + approval status
- `POST /api/agents/{agent_id}/approve` agent goedkeuren
- `POST /api/agents/{agent_id}/reject` agent afwijzen
- `POST /api/agents/{agent_id}/unapprove` agent terug op pending zetten
- `GET /api/agent-events` agent discovery/approval eventfeed

## Live Bitvavo integratie (volgende fase)

In `common/exchange/bitvavo.py` staat een placeholder. Voor echte trades voeg je toe:

- authenticated REST/WebSocket client
- order placement/cancel flow
- fill handling en fee accounting
- wallet synchronisatie met Bitvavo balances
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

