from sqlalchemy import Table, Column, Integer, String, Text, DateTime, MetaData, create_engine
from databases import Database
import datetime

DATABASE_URL = "postgresql://user:pass@postgres:5432/dbname"

database = Database(DATABASE_URL)
metadata = MetaData()

user_logs = Table(
    "user_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String),
    Column("action", String),
    Column("timestamp", DateTime, default=datetime.datetime.utcnow)
)

llm_logs = Table(
    "llm_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("prompt", Text),
    Column("response", Text),
    Column("source", String(50)),
    Column("created_at", DateTime, default=datetime.datetime.utcnow)
)

# ✅ THIS PART creates the tables in PostgreSQL:
engine = create_engine(DATABASE_URL)
metadata.create_all(engine)
