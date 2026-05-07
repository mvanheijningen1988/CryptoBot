# Changelog

All notable changes to the CryptoBot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] – 2026-05-07

### Added

#### Architecture & Infrastructure
- Manager/agent container architecture with Docker Compose
- Agent auto-discovery, heartbeat monitoring and approval workflow (pending → approved / rejected)
- Basic fail-over: manager detects heartbeat timeout and reassigns bots to another approved agent
- SQLite database with SQLAlchemy ORM and idempotent migrations
- JWT authentication with bcrypt password hashing and role-based access (admin / viewer)
- Configurable session timeout via `SESSION_MAX_HOURS` env var with automatic logout

#### Trading Engine
- Static grid trading strategy with configurable levels, price range and order sizes
- Two-phase limit order architecture: `on_price()` places initial buys, `confirm_fill()` places follow-up sells
- Simulated exchange adapter for paper trading via public Bitvavo WebSocket
- Live Bitvavo exchange adapter with authenticated WebSocket (ticker stream, order placement)
- Profit modes: `compound` (reinvest) and `skim` (partial profit withdrawal)
- Budget allocation per bot (`quote_budget` / `base_budget`)

#### Order & Trade Tracking
- Trade event persistence with `order_placed`, `order_filled` and `order_cancelled` event types
- Automatic buy↔sell order linking for PnL calculation per round-trip
- Order detail modal with linked order information
- Full decimal precision throughout (no rounding)

#### Dashboard UI
- Real-time bot overview with status, price, equity, PnL and trade count
- Notification panel with tabs for trades, orders and agent events
- Orders table showing market, type, side, price, amount and PnL (clickable for details)
- Budget trend chart per bot with starting budget, PnL and total equity labels
- Combined "Total (all bots)" equity chart option
- Trade levels chart modal with price history, grid lines and trade markers with tooltips
- Open orders modal showing grid state per bot
- Static grid profitability preview before bot creation
- Quick backtest endpoint and UI
- Real-time 24h market summary via Bitvavo WebSocket (price, change, volume)
- Market dropdown populated from Bitvavo `/v2/markets`
- Agent management: approve, reject, unapprove, view logs with live refresh
- Agent uptime display in agents table
- Custom dropdown action menus for bots and agents
- In-place DOM updates to preserve dropdown state during polling
- Toast notifications for new orders/fills only (suppressed on page reload)
- Canvas 2D charts (equity trend, trade levels) with responsive drawing

#### Authentication & Security
- Login page with username/password and forced password change on first login
- Password requirements: minimum 8 characters, at least 1 digit, at least 1 special character
- Live password rules checklist with ✓/✗ indicators during password change
- Backend password validation enforcing the same rules
- Configurable JWT session expiry (`SESSION_MAX_HOURS` env var, default 24h)
- Automatic session expiry check on dashboard init and every poll cycle
- RBAC: viewers cannot create/start/stop/delete bots

#### Internationalisation
- Full English and Dutch (NL) translations
- Language switcher with flag icons (persisted per user in database)
- All UI text, table headers, labels, toasts and modals translated

#### Developer Experience
- 286 passing tests covering auth, models, endpoints, exchange adapters, strategies and services
- Pytest configuration with `conftest.py` fixtures for database, users and API client
- `pyproject.toml` project metadata for manager, agent and root
