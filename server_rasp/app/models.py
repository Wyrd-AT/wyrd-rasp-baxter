# app/models.py (VERSÃO BAXTER 2.0 - HÍBRIDA)

import logging
from sqlalchemy import (Column, Integer, String, DateTime, JSON, 
                        create_engine, ForeignKey, Table, Float, Boolean)
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from .config import settings

logger = logging.getLogger(__name__)

# Nome do arquivo de banco de dados
db_filename = "base_rtls_baxter_v2.db"
DATABASE_URL = f"sqlite:///./{db_filename}"

logger.info(f"Iniciando conexão com o banco de dados: {db_filename}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# --- Tabela de Associação (Painel <-> Andar) ---
# Necessária para a visualização dinâmica da planta
painel_andar_association = Table('painel_andar_association', Base.metadata,
    Column('painel_id', Integer, ForeignKey('paineis_visualizacao.id'), primary_key=True),
    Column('andar_id', Integer, ForeignKey('andares.id'), primary_key=True)
)

# ==============================================================================
# TABELAS DE ESTRUTURA E VISUALIZAÇÃO
# ==============================================================================

class PainelVisualizacao(Base):
    __tablename__ = "paineis_visualizacao"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    # Define como o painel será renderizado no frontend (ex: 'multi_planta')
    tipo_layout = Column(String, nullable=False, default='multi_planta')
    
    andares = relationship("Andar", secondary=painel_andar_association, back_populates="paineis")
    
    def __str__(self):
        return self.nome

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    # URL da imagem de fundo da planta baixa deste andar
    planta_imagem_url = Column(String, nullable=True)
    
    quartos = relationship("Quarto", back_populates="andar")
    paineis = relationship("PainelVisualizacao", secondary=painel_andar_association, back_populates="andares")
    
    def __str__(self):
        return self.nome

class Quarto(Base):
    __tablename__ = "quartos"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    pos_x = Column(Float, nullable=True)
    pos_y = Column(Float, nullable=True)
    
    quarto_imagem_url = Column(String, nullable=True)
    
    # ID de Integração com o sistema Connecta (Vinculado ao Quarto)
    connecta_id = Column(String, nullable=True, unique=True, index=True)
    
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="quarto")

    def __str__(self):
        return self.nome

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

# ==============================================================================
# TABELAS DE DISPOSITIVOS E ATIVOS
# ==============================================================================

class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True) # Ex: Cama 101
    mac_beacon = Column(String, unique=True, nullable=False, index=True) # MAC BLE
    
    # Dados de hardware/rede da Cama
    mac_address = Column(String, unique=True, nullable=True, index=True) # MAC WiFi (para inventário)
    modelo = Column(String, nullable=True)
    fabricante = Column(String, nullable=True)
    
    # Estado Operacional
    status = Column(String, default='Online', nullable=False) # Online/Offline (Keepalive)
    
    # Estado de Localização (Máquina de Estados RTLS)
    # Valores: LIVRE, PENDENTE, CONFIRMADO, ALERTA
    location_status = Column(String, default='LIVRE', nullable=False)
    location_status_updated_on = Column(DateTime(timezone=True), nullable=True)

    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")

class Embarcado(Base):
    __tablename__ = "embarcados"
    id = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    mac_address = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    wifi_signal = Column(Integer, nullable=True)
    status_rede = Column(String, default="offline", nullable=False)
    
    # Configuração de sensibilidade individual
    rssi_threshold = Column(Integer, nullable=True)
    
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    quarto = relationship("Quarto", back_populates="embarcados")

# ==============================================================================
# TABELA DE HISTÓRICO
# ==============================================================================

class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id = Column(Integer, primary_key=True, index=True)
    esp_id = Column(String, nullable=False, index=True)
    ativo = Column(String, nullable=False, index=True)
    quarto_nome = Column(String, nullable=True)
    andar_nome = Column(String, nullable=True)
    action = Column(String, nullable=False, index=True) # GET, OUT, ALERTA
    status = Column(String, nullable=True, index=True) # Detalhe curto
    status_detail = Column(String, nullable=True) # Detalhe longo
    rssi = Column(Integer, nullable=True)
    wifi = Column(Integer, nullable=True)
    data_on = Column(DateTime(timezone=True), nullable=False, index=True)
    raw = Column(JSON, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)