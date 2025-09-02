# models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine, ForeignKey
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import logging
logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite:///./base_wh_baxter.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # Relação inversa para acessar os quartos de um andar
    quartos = relationship("Quarto", back_populates="andar")


class Quarto(Base):
    __tablename__ = "quartos"
    id   = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    andar = relationship("Andar", back_populates="quartos")

    embarcados = relationship("Embarcado", back_populates="quarto")
    assets     = relationship("Asset", back_populates="quarto")

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True)
    mac_beacon = Column(String, unique=True, nullable=False, index=True)
    mac_address = Column(String, unique=True, nullable=True, index=True) 
    tipo_ativo = Column(String, nullable=True)
    modelo = Column(String, nullable=True)
    fabricante = Column(String, nullable=True)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")
    status = Column(String, default='Online', nullable=False)


class Embarcado(Base):
    __tablename__ = "embarcados"
    id        = Column(Integer, primary_key=True, index=True)
    id_esp    = Column(String, unique=True, nullable=False, index=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    mac_address = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    wifi_signal = Column(Integer, nullable=True)
    status_rede = Column(String, default="offline", nullable=False) 
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    rssi_threshold = Column(Integer, nullable=True)
    quarto = relationship("Quarto", back_populates="embarcados")


class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id            = Column(Integer, primary_key=True, index=True)
    esp_id        = Column(String, nullable=False, index=True)
    ativo        = Column(String, nullable=False, index=True)
    quarto_nome   = Column(String, nullable=True) 
    andar_nome = Column(String, nullable=True)
    action        = Column(String, nullable=False, index=True)    
    status        = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)   
    rssi          = Column(Integer, nullable=True)
    wifi          = Column(Integer, nullable=True)
    data_on       = Column(DateTime(timezone=True), nullable=False, index=True)
    raw           = Column(JSON, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)