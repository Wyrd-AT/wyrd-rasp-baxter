# app/models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, create_engine, ForeignKey, Table, Float, Boolean
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from .config import settings

# Nome do arquivo de banco de dados
db_filename = "base_rtls_baxter_v2.db"
DATABASE_URL = f"sqlite:///./{db_filename}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# Tabela de Associação Painel <-> Andar
painel_andar_association = Table('painel_andar_association', Base.metadata,
    Column('painel_id', Integer, ForeignKey('paineis_visualizacao.id'), primary_key=True),
    Column('andar_id', Integer, ForeignKey('andares.id'), primary_key=True)
)

class PainelVisualizacao(Base):
    __tablename__ = "paineis_visualizacao"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    tipo_layout = Column(String, nullable=False, default='multi_planta')
    andares = relationship("Andar", secondary=painel_andar_association, back_populates="paineis")

class Andar(Base):
    __tablename__ = "andares"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    planta_imagem_url = Column(String, nullable=True)
    quartos = relationship("Quarto", back_populates="andar")
    paineis = relationship("PainelVisualizacao", secondary=painel_andar_association, back_populates="andares")

class Quarto(Base):
    __tablename__ = "quartos"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    # Coordenadas
    pos_x = Column(Float, nullable=True)
    pos_y = Column(Float, nullable=True)
    quarto_imagem_url = Column(String, nullable=True)
    # Integração (Obrigatório na regra de negócio, mas nullable no DB para evitar crash em migração)
    connecta_id = Column(String, nullable=True, unique=True, index=True)
    
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="quarto")

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True)
    
    # RTLS (O Beacon BLE) - MANTIDO
    mac_beacon = Column(String, unique=True, nullable=False, index=True)
    
    # DADOS TÉCNICOS DA CAMA (Wi-Fi/Hillrom)
    mac_address = Column(String, unique=True, nullable=True) # MAC da Placa Wi-Fi
    ip_address = Column(String, nullable=True)               # IP na rede (NOVO)
    firmware_version = Column(String, nullable=True)         # Versão FW (NOVO)
    
    # Metadados
    modelo = Column(String, nullable=True)
    fabricante = Column(String, nullable=True)
    
    # Status de Conexão (Keep-Alive)
    status = Column(String, default='Online', nullable=False)
    
    # Status de Localização
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
    rssi_threshold = Column(Integer, nullable=True)
    
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=False)
    quarto = relationship("Quarto", back_populates="embarcados")

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

def init_db():
    Base.metadata.create_all(bind=engine)