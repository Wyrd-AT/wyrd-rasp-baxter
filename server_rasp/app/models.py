# models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine, ForeignKey, Boolean, Float # Adicione Boolean e Float
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

class TipoDeAtivo(Base):
    __tablename__ = "tipos_de_ativo"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    # Regras de negócio
    requer_confirmacao_externa = Column(Boolean, default=False, nullable=False)
    precisa_de_despache = Column(Boolean, default=True, nullable=False) # Na Baxter, sempre despachamos
    algoritmo_media = Column(String, default='SMA', nullable=False)
    parametro_media = Column(Float, default=15, nullable=False)
    
    assets = relationship("Asset", back_populates="tipo_de_ativo")

class TipoDeQuarto(Base):
    __tablename__ = "tipos_de_quarto"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    # Regras de negócio
    capacidade_maxima = Column(Integer, default=1, nullable=False) # Na Baxter, a capacidade é 1
    permite_transicao_direta = Column(Boolean, default=False, nullable=False) # Na Baxter, é sempre um leito
    habilita_eventos_integracao = Column(Boolean, default=True, nullable=False) # Na Baxter, sempre integramos
    
    quartos = relationship("Quarto", back_populates="tipo_de_quarto")

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # Relação inversa para acessar os quartos de um andar
    quartos = relationship("Quarto", back_populates="andar")


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
    
    # Estes campos serão migrados e depois removidos, mas por enquanto, eles ficam.
    tipo_ativo = Column(String, nullable=True) # Campo antigo
    modelo = Column(String, nullable=True) # Campo antigo
    fabricante = Column(String, nullable=True) # Campo antigo
    
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")
    status = Column(String, default='Online', nullable=False)
    location_status = Column(String, default='Confirmado', nullable=False)
    location_status_updated_on = Column(DateTime(timezone=True), nullable=True)
    
    # --- ADIÇÃO ---
    tipo_ativo_id = Column(Integer, ForeignKey("tipos_de_ativo.id"), nullable=True)
    tipo_de_ativo = relationship("TipoDeAtivo", back_populates="assets")

class Quarto(Base):
    __tablename__ = "quartos"
    id   = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto")
    assets     = relationship("Asset", back_populates="quarto")
    
    # --- ADIÇÃO ---
    tipo_quarto_id = Column(Integer, ForeignKey("tipos_de_quarto.id"), nullable=True)
    tipo_de_quarto = relationship("TipoDeQuarto", back_populates="quartos")

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
    connecta_id = Column(String, nullable=False)


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