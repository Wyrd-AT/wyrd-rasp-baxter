# models.py

from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./beds.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class Bed(Base):
    __tablename__ = "beds"
    id          = Column(Integer, primary_key=True, index=True)
    mac_address = Column(String, unique=True, nullable=False, index=True)
    nome_cama   = Column(String, nullable=False)
    mac_beacon  = Column(String, nullable=True)
    quarto      = Column(String, nullable=True)

class Embarcado(Base):
    __tablename__ = "embarcados"
    id     = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    quarto = Column(String, nullable=False)

class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id       = Column(Integer, primary_key=True, index=True)
    esp_id   = Column(String, nullable=False, index=True)
    cama     = Column(String, nullable=False, index=True)
    status   = Column(String, nullable=False, index=True)
    rssi     = Column(Integer, nullable=True)
    wifi     = Column(Integer, nullable=True)
    # Usamos data_on como timestamp principal
    data_on  = Column(DateTime(timezone=True), nullable=False, index=True)
    raw      = Column(JSON, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)
