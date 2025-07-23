# models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./wh_connect.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# NOVO MODELO PARA CONFIGURAÇÕES GLOBAIS
class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Badge(Base):
    __tablename__ = "badges"
    id          = Column(Integer, primary_key=True, index=True)
    nome_cracha = Column(String, nullable=False, unique=True)
    mac_beacon  = Column(String, unique=True, nullable=False, index=True)
    quarto      = Column(String, nullable=True)

class Embarcado(Base):
    __tablename__ = "embarcados"
    id     = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    quarto = Column(String, nullable=False)
    andar  = Column(String, nullable=True)
    # As colunas de configuração foram REMOVIDAS daqui.

class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id            = Column(Integer, primary_key=True, index=True)
    esp_id        = Column(String, nullable=False, index=True)
    cracha          = Column(String, nullable=False, index=True)
    action        = Column(String, nullable=False, index=True)    
    status        = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)   
    rssi          = Column(Integer, nullable=True)
    wifi          = Column(Integer, nullable=True)
    data_on       = Column(DateTime(timezone=True), nullable=False, index=True)
    raw           = Column(JSON, nullable=False)
def init_db():
    Base.metadata.create_all(bind=engine)
