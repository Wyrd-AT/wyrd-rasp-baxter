# models.py
from sqlalchemy import (Column, DateTime, ForeignKey, Integer, Text, Float, JSON, String,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite:///./base_rtls_rfid.db"
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
    quartos = relationship("Quarto", back_populates="andar")

class Quarto(Base):
    __tablename__ = "quartos"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    andar_id = Column(Integer, ForeignKey("andares.id"), nullable=False)
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto")
    assets = relationship("Asset", back_populates="quarto")

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

# ====================================================
#         NOVOS MODELOS DE INVENTÁRIO E CATÁLOGO
# ====================================================

# --- ENTIDADES DE SUPORTE (AS OPÇÕES DOS DROPDOWNS) ---

class Fabricante(Base):
    __tablename__ = "fabricantes"
    id = Column(Integer, primary_key=True)
    nome = Column(String(100), unique=True, nullable=False)
    
    # --- CORREÇÃO AQUI ---
    # A relação agora é com os "Modelos de Equipamento", e não com as instâncias.
    equipamento_tipos = relationship("EquipamentoTipo", back_populates="fabricante")

class TipoPlaca(Base):
    __tablename__ = "tipos_placa"
    id = Column(Integer, primary_key=True)
    nome = Column(String(100), unique=True, nullable=False)

# --- NÍVEL 1: LOCALIZAÇÃO (DATACENTER) ---
class DataCenter(Base):
    __tablename__ = "datacenters"
    id = Column(Integer, primary_key=True)
    nome = Column(String(100), unique=True, nullable=False) # O nome que o usuário vê
    uf_abrv = Column(String(2))
    estacao_abrv = Column(String(20))
    edf = Column(String(50))
    piso = Column(String(20))
    sala = Column(String(50))
    municipio = Column(String(100))
    endereco_completo = Column(Text)
    latitude = Column(Float)
    longitude = Column(Float)
    snapshots = relationship("InventorySnapshot", back_populates="datacenter")
    bastidores = relationship("Bastidor", back_populates="localizacao")

class Bastidor(Base): # Rack
    __tablename__ = "bastidores"
    id = Column(Integer, primary_key=True)
    codigo_bast = Column(String(50), unique=True, nullable=False)
    localizacao_id = Column(Integer, ForeignKey("datacenters.id"), nullable=False)
    localizacao = relationship("DataCenter", back_populates="bastidores")
    # Relação com as instâncias de equipamento
    equipamento_instancias = relationship("Equipamento", back_populates="bastidor")


# --- NOVA TABELA: O "MOLDE" / MODELO DO EQUIPAMENTO ---
class EquipamentoTipo(Base):
    __tablename__ = "equipamento_tipos"
    id = Column(Integer, primary_key=True)
    nome = Column(String(100), unique=True, nullable=False) # Ex: "Roteador ASR-9006"
    
    # Propriedades fixas do modelo
    modelo = Column(String(100))
    tecnologia_equip = Column(String(50))
    tipo_equip = Column(String(50))
    estado_cv_equip = Column(String(50))
    estado_op_equip = Column(String(50))

    # Relação com Fabricante
    fabricante_id = Column(Integer, ForeignKey("fabricantes.id"))
    fabricante = relationship("Fabricante", back_populates="equipamento_tipos")
    
    # Relação com as instâncias criadas a partir deste tipo
    instancias = relationship("Equipamento", back_populates="equipamento_tipo")


# --- TABELA EQUIPAMENTO (AGORA REPRESENTA A INSTÂNCIA FÍSICA) ---
class Equipamento(Base):
    __tablename__ = "equipamentos"
    id = Column(Integer, primary_key=True)
    nome_equip = Column(String(100), unique=True) # Hostname único da instância, ex: "RB1.SP.SAO.EDF01-RT01"
    
    # --- RELAÇÕES ---
    # A qual "molde" esta instância pertence?
    equipamento_tipo_id = Column(Integer, ForeignKey("equipamento_tipos.id"), nullable=False)
    equipamento_tipo = relationship("EquipamentoTipo", back_populates="instancias")
    
    # Onde esta instância está fisicamente?
    bastidor_id = Column(Integer, ForeignKey("bastidores.id"))
    bastidor = relationship("Bastidor", back_populates="equipamento_instancias")
    
    # Qual etiqueta RFID está colada nesta instância? (Relação 1-para-1)
    product = relationship("Product", back_populates="equipamento", uselist=False, cascade="all, delete-orphan")


# --- A ETIQUETA RFID (AGORA LIGADA À INSTÂNCIA DO EQUIPAMENTO) ---
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    codigo_rfid = Column(String, unique=True, nullable=False, index=True)
    
    equipamento_id = Column(Integer, ForeignKey("equipamentos.id"), unique=True, nullable=True)
    equipamento = relationship("Equipamento", back_populates="product")
    
    inventory_entries = relationship("InventoryItem", back_populates="product")

# --- MODELOS DO SISTEMA DE INVENTÁRIO (SNAPSHOTS) ---
class InventorySnapshot(Base):
    __tablename__ = 'inventory_snapshots'
    id = Column(Integer, primary_key=True)
    created_on = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    datacenter_id = Column(Integer, ForeignKey("datacenters.id"), nullable=False)
    datacenter = relationship("DataCenter", back_populates="snapshots") 
    items = relationship('InventoryItem', back_populates='snapshot', cascade="all, delete-orphan")

class InventoryItem(Base):
    __tablename__ = 'inventory_items'
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey('inventory_snapshots.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    snapshot = relationship('InventorySnapshot', back_populates='items')
    product = relationship('Product', back_populates='inventory_entries')

def init_db():
    """Inicializa o banco de dados e cria as tabelas."""
    Base.metadata.create_all(bind=engine)