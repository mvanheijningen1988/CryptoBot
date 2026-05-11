from __future__ import annotations

from sqlalchemy.orm import sessionmaker
from sqlalchemy import func

from manager.app.events import add_trade_event
from manager.app.models import TradeEvent


def test_sell_event_links_to_buy_fill_from_previous_grid_level(db_engine, monkeypatch):
    test_session = sessionmaker(bind=db_engine)
    monkeypatch.setattr("manager.app.events.SessionLocal", test_session)

    buy_id = add_trade_event(
        bot_id="bot-1",
        bot_name="Bot 1",
        side="buy",
        quote_amount=100.0,
        price=1.0,
        trade_pnl=0.0,
        total_equity=1000.0,
        trade_number=1,
        event_type="order_filled",
        level_index=1,
        market="TEST-EUR",
    )
    sell_id = add_trade_event(
        bot_id="bot-1",
        bot_name="Bot 1",
        side="sell",
        quote_amount=100.0,
        price=2.0,
        trade_pnl=10.0,
        total_equity=1010.0,
        trade_number=2,
        event_type="order_placed",
        level_index=2,
        market="TEST-EUR",
    )

    session = test_session()
    try:
        buy = session.query(TradeEvent).filter(TradeEvent.id == buy_id).first()
        sell = session.query(TradeEvent).filter(TradeEvent.id == sell_id).first()
        assert sell is not None
        assert buy is not None
        assert sell.linked_order_id == buy_id
        assert buy.linked_order_id == sell_id
    finally:
        session.close()


def test_buy_fill_links_to_existing_sell_on_next_level(db_engine, monkeypatch):
    test_session = sessionmaker(bind=db_engine)
    monkeypatch.setattr("manager.app.events.SessionLocal", test_session)

    sell_id = add_trade_event(
        bot_id="bot-2",
        bot_name="Bot 2",
        side="sell",
        quote_amount=100.0,
        price=2.0,
        trade_pnl=0.0,
        total_equity=1000.0,
        trade_number=2,
        event_type="order_placed",
        level_index=2,
        market="TEST-EUR",
    )
    buy_id = add_trade_event(
        bot_id="bot-2",
        bot_name="Bot 2",
        side="buy",
        quote_amount=100.0,
        price=1.0,
        trade_pnl=0.0,
        total_equity=1000.0,
        trade_number=1,
        event_type="order_filled",
        level_index=1,
        market="TEST-EUR",
    )

    session = test_session()
    try:
        buy = session.query(TradeEvent).filter(TradeEvent.id == buy_id).first()
        sell = session.query(TradeEvent).filter(TradeEvent.id == sell_id).first()
        assert buy is not None
        assert sell is not None
        assert buy.linked_order_id == sell_id
        assert sell.linked_order_id == buy_id
    finally:
        session.close()


def test_order_filled_updates_existing_order_row_by_order_id(db_engine, monkeypatch):
    test_session = sessionmaker(bind=db_engine)
    monkeypatch.setattr("manager.app.events.SessionLocal", test_session)

    placed_id = add_trade_event(
        bot_id="bot-3",
        bot_name="Bot 3",
        side="buy",
        quote_amount=100.0,
        price=1.0,
        trade_pnl=0.0,
        total_equity=1000.0,
        trade_number=1,
        event_type="order_placed",
        level_index=1,
        market="TEST-EUR",
        order_id="ord-123",
    )
    filled_id = add_trade_event(
        bot_id="bot-3",
        bot_name="Bot 3",
        side="buy",
        quote_amount=100.0,
        price=1.0,
        trade_pnl=1.23,
        total_equity=1001.23,
        trade_number=2,
        event_type="order_filled",
        level_index=1,
        market="TEST-EUR",
        order_id="ord-123",
    )

    session = test_session()
    try:
        assert placed_id == filled_id
        count = session.query(func.count(TradeEvent.id)).filter(TradeEvent.bot_id == "bot-3").scalar()
        assert count == 1
        row = session.query(TradeEvent).filter(TradeEvent.id == placed_id).first()
        assert row is not None
        assert row.event_type == "order_filled"
        assert row.order_id == "ord-123"
        assert row.trade_pnl == 1.23
    finally:
        session.close()


def test_trade_event_persists_exchange_order_id(db_engine, monkeypatch):
    test_session = sessionmaker(bind=db_engine)
    monkeypatch.setattr("manager.app.events.SessionLocal", test_session)

    event_id = add_trade_event(
        bot_id="bot-4",
        bot_name="Bot 4",
        side="sell",
        quote_amount=50.0,
        price=2.0,
        trade_pnl=0.5,
        total_equity=1002.0,
        trade_number=9,
        event_type="order_filled",
        level_index=2,
        market="TEST-EUR",
        order_id="local-ord-1",
        exchange_order_id="00000000-0000-0463-0100-00078fab0b8c",
    )

    session = test_session()
    try:
        row = session.query(TradeEvent).filter(TradeEvent.id == event_id).first()
        assert row is not None
        assert row.order_id == "local-ord-1"
        assert row.exchange_order_id == "00000000-0000-0463-0100-00078fab0b8c"
    finally:
        session.close()