/**
 * CryptoBot Manager – Internationalisation (i18n) Dictionary
 *
 * Maps translation keys to localised strings for English (en) and
 * Dutch (nl).  Used by both login-app.js and dashboard.js via the
 * global I18N constant and the t(key) helper function.
 *
 * To add a new language, append another property to the I18N object
 * with the ISO 639-1 code as key and a full set of translations.
 */
const I18N = {
  // ── English translations ──────────────────────────────────────
  en: {
    // Header / page title
    app_title: "CryptoBot Manager Dashboard",

    // Tabs
    tab_dashboard: "Dashboard",
    tab_agents: "Agents",

    // Dashboard
    btn_new_bot: "+ New Bot",
    bots_title: "Bots",
    th_name: "Name",
    th_status: "Status",
    th_equity: "Equity",
    th_pnl: "PnL",
    th_actions: "Actions",
    btn_start: "Start",
    btn_stop: "Stop",
    notifications_title: "Agent Notifications",
    backtest_title: "Backtest (quick)",
    btn_run_backtest: "Run backtest on current form",

    // Create Bot modal
    create_bot_title: "Create Bot",
    lbl_name: "Name",
    lbl_market: "Market",
    lbl_mode: "Mode",
    market_summary: "Market summary (24h)",
    lbl_last_price: "Last price",
    lbl_24h_change: "24h change",
    lbl_24h_vol_base: "24h volume (base)",
    lbl_24h_vol_quote: "24h volume (quote)",
    lbl_start_price: "Start price",
    tip_start_price: "The starting price for simulation. In live mode the real market price is used.",
    lbl_grid: "Grid",
    lbl_lower: "Lower",
    tip_lower: "The lower bound of the grid. Orders are only placed at or above this price.",
    lbl_upper: "Upper",
    tip_upper: "The upper bound of the grid. Orders are only placed at or below this price.",
    lbl_levels: "Levels",
    tip_levels: "The number of grid levels (price lines) between lower and upper. More levels = smaller steps, more orders.",
    lbl_order_size: "Order size (quote)",
    tip_order_size: "The amount in quote currency (e.g. EUR) used per individual grid order.",
    lbl_fee_rate: "Fee rate (%)",
    tip_fee_rate: "The fee percentage charged by the exchange per trade (e.g. 0.25% on Bitvavo).",
    btn_check_profit: "Check grid profitability",
    no_calculation: "No calculation yet.",
    lbl_budget: "Budget",
    lbl_available: "Available:",
    lbl_quote: "quote",
    lbl_base: "base",
    lbl_quote_budget: "Quote budget",
    tip_quote_budget: "The total amount in quote currency (e.g. EUR) the bot may use for placing buy orders. Automatically filled with available balance.",
    lbl_profit_mode: "Profit mode",
    tip_profit_mode: "Withdraw: full profit is withdrawn to the wallet. Compound: reinvests profit in the grid. Skim: takes part of the profit aside based on skim ratio.",
    lbl_skim_ratio: "Skim ratio",
    tip_skim_ratio: "The portion of profit set aside (e.g. 0.5 = 50% profit skimmed). Only active in 'skim' profit mode.",
    btn_create_bot: "Create bot",
    btn_cancel: "Cancel",

    // Grid profitability results
    grid_profitable: "Grid appears profitable",
    grid_not_profitable: "Grid does not appear profitable",
    grid_step_size: "Step size",
    grid_profit_per_trade: "Profit per trade (quote): min",
    grid_avg: "avg",
    grid_max: "max",
    grid_profitable_paths: "Profitable paths",
    grid_used_fee: "Used fee rate",
    grid_calc_error: "Could not calculate profitability preview",
    grid_confirm_unprofitable: "This grid does not appear profitable based on fee + step size. Create bot anyway?",

    // Agents tab
    agents_title: "Agents",
    th_bots: "Bots",
    th_version: "Version",
    th_approval: "Approval",
    btn_approve: "Approve",
    btn_reject: "Reject",
    btn_unapprove: "Unapprove",
    btn_open_logs: "Open logs",

    // Agent popup
    popup_new_agent: "New agent discovered",
    btn_close: "Close",

    // Agent logs modal
    logs_title: "Agent Logs Stream",
    lbl_agent: "Agent",
    lbl_category: "Category",
    lbl_all: "all",
    lbl_system: "system",
    lbl_trading: "trading",
    lbl_max_logs: "Max logs",
    btn_refresh: "Refresh now",
    logs_none: "No logs available yet.",
    logs_no_agent: "No agent selected.",
    logs_error: "Could not fetch logs",

    // Auth
    login_title: "Login",
    lbl_username: "Username",
    lbl_password: "Password",
    btn_login: "Login",
    login_error: "Invalid username or password",
    change_pw_title: "Change Password",
    change_pw_msg: "You must change your password before continuing.",
    lbl_new_password: "New password",
    lbl_confirm_password: "Confirm password",
    btn_change_pw: "Change password",
    pw_mismatch: "Passwords do not match",
    pw_too_short: "Password must be at least 6 characters",

    // User menu
    btn_logout: "Logout",
    lbl_language: "Language",
  },

  // ── Dutch (Nederlands) translations ────────────────────────────
  nl: {
    // Header / page title
    app_title: "CryptoBot Manager Dashboard",

    // Tabs
    tab_dashboard: "Dashboard",
    tab_agents: "Agents",

    // Dashboard
    btn_new_bot: "+ Nieuwe Bot",
    bots_title: "Bots",
    th_name: "Naam",
    th_status: "Status",
    th_equity: "Vermogen",
    th_pnl: "W&V",
    th_actions: "Acties",
    btn_start: "Start",
    btn_stop: "Stop",
    notifications_title: "Agent Notificaties",
    backtest_title: "Backtest (snel)",
    btn_run_backtest: "Backtest uitvoeren op huidig formulier",

    // Create Bot modal
    create_bot_title: "Bot Aanmaken",
    lbl_name: "Naam",
    lbl_market: "Markt",
    lbl_mode: "Modus",
    market_summary: "Marktoverzicht (24u)",
    lbl_last_price: "Laatste prijs",
    lbl_24h_change: "24u verandering",
    lbl_24h_vol_base: "24u volume (base)",
    lbl_24h_vol_quote: "24u volume (quote)",
    lbl_start_price: "Startprijs",
    tip_start_price: "De startkoers voor simulatie. In live mode wordt de echte marktprijs gebruikt.",
    lbl_grid: "Grid",
    lbl_lower: "Ondergrens",
    tip_lower: "De ondergrens van het grid. Orders worden alleen geplaatst boven of op deze prijs.",
    lbl_upper: "Bovengrens",
    tip_upper: "De bovengrens van het grid. Orders worden alleen geplaatst onder of op deze prijs.",
    lbl_levels: "Niveaus",
    tip_levels: "Het aantal grid-niveaus (prijslijnen) tussen onder- en bovengrens. Meer niveaus = kleinere stappen, meer orders.",
    lbl_order_size: "Ordergrootte (quote)",
    tip_order_size: "Het bedrag in quote-valuta (bijv. EUR) dat per individuele grid-order wordt ingezet.",
    lbl_fee_rate: "Fee rate (%)",
    tip_fee_rate: "Het fee-percentage dat de exchange per trade in rekening brengt (bijv. 0.25% bij Bitvavo).",
    btn_check_profit: "Grid winstgevendheid controleren",
    no_calculation: "Nog geen berekening.",
    lbl_budget: "Budget",
    lbl_available: "Beschikbaar:",
    lbl_quote: "quote",
    lbl_base: "base",
    lbl_quote_budget: "Quote budget",
    tip_quote_budget: "Het totale bedrag in quote-valuta (bijv. EUR) dat de bot mag gebruiken voor het plaatsen van buy-orders. Wordt automatisch gevuld met het beschikbare saldo.",
    lbl_profit_mode: "Winstmodus",
    tip_profit_mode: "Withdraw: volledige winst wordt teruggetrokken naar de wallet. Compound: herinvesteert winst in het grid. Skim: neemt een deel van de winst apart op basis van de skim ratio.",
    lbl_skim_ratio: "Skim ratio",
    tip_skim_ratio: "Het deel van de winst dat apart wordt gezet (bijv. 0.5 = 50% winst afromen). Alleen actief bij profit mode 'skim'.",
    btn_create_bot: "Bot aanmaken",
    btn_cancel: "Annuleren",

    // Grid profitability results
    grid_profitable: "Grid lijkt winstgevend",
    grid_not_profitable: "Grid lijkt niet winstgevend",
    grid_step_size: "Stapgrootte",
    grid_profit_per_trade: "Winst per trade (quote): min",
    grid_avg: "gem",
    grid_max: "max",
    grid_profitable_paths: "Winstgevende paden",
    grid_used_fee: "Gebruikte fee rate",
    grid_calc_error: "Kon winstgevendheid niet berekenen",
    grid_confirm_unprofitable: "Deze grid lijkt niet winstgevend op basis van fee + stapgrootte. Toch bot aanmaken?",

    // Agents tab
    agents_title: "Agents",
    th_bots: "Bots",
    th_version: "Versie",
    th_approval: "Goedkeuring",
    btn_approve: "Goedkeuren",
    btn_reject: "Afwijzen",
    btn_unapprove: "Intrekken",
    btn_open_logs: "Logs openen",

    // Agent popup
    popup_new_agent: "Nieuwe agent ontdekt",
    btn_close: "Sluiten",

    // Agent logs modal
    logs_title: "Agent Logs Stream",
    lbl_agent: "Agent",
    lbl_category: "Categorie",
    lbl_all: "alle",
    lbl_system: "systeem",
    lbl_trading: "trading",
    lbl_max_logs: "Max logs",
    btn_refresh: "Nu verversen",
    logs_none: "Nog geen logs beschikbaar.",
    logs_no_agent: "Geen agent geselecteerd.",
    logs_error: "Kon logs niet ophalen",

    // Auth
    login_title: "Inloggen",
    lbl_username: "Gebruikersnaam",
    lbl_password: "Wachtwoord",
    btn_login: "Inloggen",
    login_error: "Ongeldige gebruikersnaam of wachtwoord",
    change_pw_title: "Wachtwoord Wijzigen",
    change_pw_msg: "U moet uw wachtwoord wijzigen voordat u verder kunt.",
    lbl_new_password: "Nieuw wachtwoord",
    lbl_confirm_password: "Bevestig wachtwoord",
    btn_change_pw: "Wachtwoord wijzigen",
    pw_mismatch: "Wachtwoorden komen niet overeen",
    pw_too_short: "Wachtwoord moet minimaal 6 tekens zijn",

    // User menu
    btn_logout: "Uitloggen",
    lbl_language: "Taal",
  },
};
