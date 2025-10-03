import logging
from sqlalchemy import (Column, Integer, String, DateTime, JSON, 
                        create_engine, ForeignKey, Table, Float)
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# 1. Importar as configurações do seu config.py
from .config import settings

logger = logging.getLogger(__name__)

# 2. Ler o nome do cliente do config.ini
version_name = settings.get('version', 'default').lower() # 'default' é um fallback de segurança

# 3. Montar o nome do arquivo do banco de dados dinamicamente
db_filename = f"base_rtls_{version_name}.db"
DATABASE_URL = f"sqlite:///./{db_filename}"

logger.info(f"Iniciando conexão com o banco de dados: {db_filename}")

# O resto do código continua igual, usando a DATABASE_URL que acabamos de criar
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


# --- Tabela de Associação ---

painel_andar_association = Table('painel_andar_association', Base.metadata,
    Column('painel_id', Integer, ForeignKey('paineis_visualizacao.id'), primary_key=True),
    Column('andar_id', Integer, ForeignKey('andares.id'), primary_key=True)
)


# --- Modelo de Painéis de Visualização ---

class PainelVisualizacao(Base):
    __tablename__ = "paineis_visualizacao"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    tipo_layout = Column(String, nullable=False)
    ordem_exibicao = Column(Integer, default=0)
    
    andares = relationship("Andar", secondary=painel_andar_association, back_populates="paineis")

    def __str__(self):
        return self.nome


# --- Modelos de Localização ---

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
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
    
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto")
    assets = relationship("Asset", back_populates="quarto")

    def __str__(self):
        return self.nome


# --- Modelos Principais ---

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True)
    mac_beacon = Column(String, unique=True, nullable=False, index=True)
    tipo_ativo = Column(String, nullable=True)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    quarto = relationship("Quarto", back_populates="assets")
    status = Column(String, default='Online', nullable=False)

class Embarcado(Base):
    __tablename__ = "embarcados"
    id = Column(Integer, primary_key=True, index=True)
    id_esp = Column(String, unique=True, nullable=False, index=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    mac_address = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    wifi_signal = Column(Integer, nullable=True)
    status_rede = Column(String, default="offline", nullable=False)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    rssi_threshold = Column(Integer, nullable=True)
    quarto = relationship("Quarto", back_populates="embarcados")


# --- Modelo de Histórico ---

class ReceivedEvent(Base):
    __tablename__ = "received_events"
    id = Column(Integer, primary_key=True, index=True)
    esp_id = Column(String, nullable=False, index=True)
    ativo = Column(String, nullable=False, index=True)
    quarto_nome = Column(String, nullable=True)
    andar_nome = Column(String, nullable=True)
    action = Column(String, nullable=False, index=True)
    status = Column(String, nullable=True, index=True)
    status_detail = Column(String, nullable=True)
    rssi = Column(Integer, nullable=True)
    wifi = Column(Integer, nullable=True)
    data_on = Column(DateTime(timezone=True), nullable=False, index=True)
    raw = Column(JSON, nullable=False)


# --- Inicialização do Banco de Dados ---

def init_db():
    Base.metadata.create_all(bind=engine)