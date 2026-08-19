import os
import socket
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

# Load env variables
load_dotenv()

def get_db_url():
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres_pass")
    db_name = os.getenv("DB_NAME", "fno_db")
    port = os.getenv("DB_PORT", "5432")
    
    # Check if we can resolve 'db' host
    host = os.getenv("DB_HOST", "db")
    if host == "db":
        try:
            socket.gethostbyname("db")
            # If we are inside docker and 'db' is resolvable, use the standard internal port 5432
            port = "5432"
        except socket.gaierror:
            # Fallback to localhost if 'db' is not resolvable (i.e. running outside docker)
            host = "localhost"
            
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

engine = create_engine(get_db_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()

def init_db():
    # This will create tables if they do not exist
    Base.metadata.create_all(bind=engine)
    
    inspector = inspect(engine)
    
    # 1. Ensure analysed_by_ai and stage diagnostics columns exist in messages table
    if 'messages' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('messages')]
        if 'analysed_by_ai' not in columns:
            print("[*] Migration: Adding analysed_by_ai column to messages table...")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN analysed_by_ai BOOLEAN DEFAULT FALSE"))
                try:
                    conn.execute(text("CREATE INDEX ix_messages_analysed_by_ai ON messages (analysed_by_ai)"))
                except Exception as ie:
                    print(f"Index creation warning/error: {ie}")
            print("[+] Migration completed successfully.")

        msg_extra_cols = {
            "revision": "INTEGER DEFAULT 0",
            "last_stage": "VARCHAR",
            "last_status": "VARCHAR",
            "last_error": "TEXT"
        }
        with engine.begin() as conn:
            for col_name, col_type in msg_extra_cols.items():
                if col_name not in columns:
                    print(f"[*] Migration: Adding {col_name} column to messages table...")
                    try:
                        conn.execute(text(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}"))
                    except Exception as me:
                        print(f"Migration column error ({col_name}): {me}")

    # 2. Ensure Zerodha execution columns exist in actions table
    if 'actions' in inspector.get_table_names():
        existing_cols = [col['name'] for col in inspector.get_columns('actions')]
        new_cols = {
            "is_main": "BOOLEAN DEFAULT TRUE",
            "is_adjustment": "BOOLEAN DEFAULT FALSE",
            "adjustment_number": "INTEGER",
            "underlying": "VARCHAR",
            "option_type": "VARCHAR",
            "strike": "DOUBLE PRECISION",
            "expiry": "VARCHAR",
            "lots": "INTEGER DEFAULT 1",
            "quantity": "INTEGER",
            "tradingsymbol": "VARCHAR",
            "instrument_token": "INTEGER",
            "transaction_type": "VARCHAR",
            "order_type": "VARCHAR",
            "product": "VARCHAR DEFAULT 'NRML'",
            "order_status": "VARCHAR DEFAULT 'PENDING'",
            "filled_quantity": "INTEGER DEFAULT 0",
            "pending_quantity": "INTEGER DEFAULT 0",
            "average_price": "DOUBLE PRECISION",
            "last_reconciled_at": "TIMESTAMP",
            "rejection_reason": "TEXT",
            "error_category": "VARCHAR",
            "zerodha_order_id": "VARCHAR",
            "zerodha_response": "TEXT",
            "placed_at": "TIMESTAMP",
            "sl_trigger_type": "VARCHAR",
            "sl_trigger_price": "DOUBLE PRECISION",
            "sl_trigger_direction": "VARCHAR",
            "sl_monitoring_active": "BOOLEAN DEFAULT FALSE",
            "sl_triggered": "BOOLEAN DEFAULT FALSE",
            "sl_triggered_at": "TIMESTAMP",
            "sl_order_id": "VARCHAR",
            "sl_order_status": "VARCHAR"
        }

        with engine.begin() as conn:
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    print(f"[*] Migration: Adding {col_name} column to actions table...")
                    try:
                        conn.execute(text(f"ALTER TABLE actions ADD COLUMN {col_name} {col_type}"))
                    except Exception as me:
                        print(f"Migration column error ({col_name}): {me}")

    # 3. Ensure Trade adjustment lifecycle columns exist in trades table
    if 'trades' in inspector.get_table_names():
        existing_trade_cols = [col['name'] for col in inspector.get_columns('trades')]
        trade_new_cols = {
            "max_adjustments": "INTEGER DEFAULT 1",
            "adjustment_count": "INTEGER DEFAULT 0",
            "last_adjustment_at": "TIMESTAMP",
            "last_adjustment_price": "DOUBLE PRECISION",
            "last_adjustment_strike": "DOUBLE PRECISION"
        }

        with engine.begin() as conn:
            for col_name, col_type in trade_new_cols.items():
                if col_name not in existing_trade_cols:
                    print(f"[*] Migration: Adding {col_name} column to trades table...")
                    try:
                        conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"))
                    except Exception as me:
                        print(f"Migration column error ({col_name}): {me}")
