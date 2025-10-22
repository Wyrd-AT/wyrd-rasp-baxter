# app/models.py (VERSÃO UNIFICADA E FINAL)

import logging
from sqlalchemy import (Column, Integer, String, DateTime, JSON, 
                        create_engine, ForeignKey, Table, Float, Boolean) # Adicionado 'Boolean'
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from .config import settings

logger = logging.getLogger(__name__)

version_name = settings.get('version', 'default').lower()
db_filename = f"base_rtls_{version_name}.db"
DATABASE_URL = f"sqlite:///./{db_filename}"

logger.info(f"Iniciando conexão com o banco de dados: {db_filename}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# --- Tabela de Associação (Painel <-> Andar) - Sem alterações ---
painel_andar_association = Table('painel_andar_association', Base.metadata,
    Column('painel_id', Integer, ForeignKey('paineis_visualizacao.id'), primary_key=True),
    Column('andar_id', Integer, ForeignKey('andares.id'), primary_key=True)
)

# ==============================================================================
# NOVAS TABELAS DE CONFIGURAÇÃO DE COMPORTAMENTO
# ==============================================================================

class TipoDeAtivo(Base):
    __tablename__ = "tipos_de_ativo"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # Define se o ativo seguirá o fluxo de confirmação em 2 etapas (Pendente -> Confirmado)
    requer_confirmacao_externa = Column(Boolean, default=False, nullable=False)
    precisa_de_despache = Column(Boolean, default=False, nullable=False)
    
    # Define o algoritmo de suavização de sinal a ser usado
    algoritmo_media = Column(String, default='SMA', nullable=False) # 'SMA' ou 'EMA'
    
    # Parâmetro para o algoritmo (Nº de amostras para SMA, fator Alpha para EMA)
    parametro_media = Column(Float, default=15, nullable=False)

    # Relação inversa para ver todos os ativos deste tipo
    assets = relationship("Asset", back_populates="tipo_de_ativo")
    
    def __str__(self):
        return self.nome

class TipoDeQuarto(Base):
    __tablename__ = "tipos_de_quarto"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    
    # Define quantos ativos podem ocupar o quarto simultaneamente (0 para ilimitado)
    capacidade_maxima = Column(Integer, default=0, nullable=False)
    
    # Define se um ativo pode se mover diretamente para um quarto vizinho (usando margem de conflito)
    # ou se precisa obrigatoriamente de um evento de "SAÍDA" antes.
    permite_transicao_direta = Column(Boolean, default=True, nullable=False)

    habilita_eventos_integracao = Column(Boolean, default=False, nullable=False)
    
    # Relação inversa para ver todos os quartos deste tipo
    quartos = relationship("Quarto", back_populates="tipo_de_quarto")
    
    def __str__(self):
        return self.nome

# ==============================================================================
# TABELAS EXISTENTES MODIFICADAS E UNIFICADAS
# ==============================================================================

class PainelVisualizacao(Base):
    __tablename__ = "paineis_visualizacao"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    tipo_layout = Column(String, nullable=False)
    andares = relationship("Andar", secondary=painel_andar_association, back_populates="paineis")
    def __str__(self):
        return self.nome

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
    
    # ADICIONADO: Chave estrangeira para o Tipo de Quarto
    tipo_quarto_id = Column(Integer, ForeignKey("tipos_de_quarto.id"), nullable=True)
    
    andar = relationship("Andar", back_populates="quartos")
    embarcados = relationship("Embarcado", back_populates="quarto", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="quarto")
    
    # ADICIONADO: Relação para acessar o objeto TipoDeQuarto
    tipo_de_quarto = relationship("TipoDeQuarto", back_populates="quartos")

    def __str__(self):
        return self.nome

class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    nome_ativo = Column(String, nullable=False, unique=True)
    mac_beacon = Column(String, unique=True, nullable=False, index=True)
    quarto_id = Column(Integer, ForeignKey("quartos.id"), nullable=True)
    status = Column(String, default='Online', nullable=False)

    # ADICIONADO (da versão Baxter): Campos de metadados do ativo
    mac_address = Column(String, unique=True, nullable=True, index=True)
    modelo = Column(String, nullable=True)
    fabricante = Column(String, nullable=True)
    
    # ADICIONADO (da versão Baxter): Campos para o fluxo de confirmação em 2 etapas
    location_status = Column(String, default='Confirmado', nullable=False)
    location_status_updated_on = Column(DateTime(timezone=True), nullable=True)

    # ADICIONADO: Chave estrangeira para o Tipo de Ativo
    tipo_ativo_id = Column(Integer, ForeignKey("tipos_de_ativo.id"), nullable=True)

    quarto = relationship("Quarto", back_populates="assets")
    
    # ADICIONADO: Relação para acessar o objeto TipoDeAtivo
    tipo_de_ativo = relationship("TipoDeAtivo", back_populates="assets")

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
    
    # ADICIONADO (da versão Baxter): ID para integração com sistema externo
    connecta_id = Column(String, nullable=True)
    
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