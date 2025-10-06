# main.py (Versão Final, Completa e Consolidada para Multi-Ativo)

import asyncio
import logging 
from .logging_config import setup_logging
from . import aggregator 

setup_logging() 

logger = logging.getLogger(__name__)

import threading
import time
import uvicorn
import csv
from io import StringIO
import json
import sys
import os
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket, WebSocketDisconnect, BackgroundTasks
from .connection_manager import manager
from . import scan_rfid 
from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_, desc, asc

from urllib.parse import urlencode
from pydantic import BaseModel

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest
from starlette.exceptions import WebSocketException 
from starlette.datastructures import URL

# --- Importações dos Módulos da Aplicação ---
from .models import (
    engine, SessionLocal, Asset, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, Andar, 
    Product, InventorySnapshot, InventoryItem, 
    DataCenter, Fabricante, Bastidor, Equipamento, EquipamentoTipo,
    init_db
)
from .services import synchronize_and_reset_esp, release_assets_for_offline_esp
from . import mqtt_client
from .aggregator import main_aggregator_loop, _asset_realtime_state
from .config import settings
from .auth import authenticate_admin

logger.info("[main] Módulo carregado para a versão MULTI-ATIVO.")

# --- Constantes e Configuração Inicial ---
HISTORY_RETENTION_DAYS = 7
EVENT_PAGE_SIZE = 25
CLEANUP_INTERVAL_SEC = 3600
NUM_FIXED_ROOMS = 6

try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

init_db()

class FabricanteBase(BaseModel):
    nome: str

class FabricanteCreate(FabricanteBase):
    pass

class FabricanteSchema(FabricanteBase):
    id: int
    class Config:
        from_attributes = True

# --- SCHEMAS ATUALIZADOS PARA DATACENTER (LOCALIZAÇÃO) ---

class DataCenterBase(BaseModel):
    nome: str
    uf_abrv: Optional[str] = None
    estacao_abrv: Optional[str] = None
    edf: Optional[str] = None
    piso: Optional[str] = None
    sala: Optional[str] = None
    municipio: Optional[str] = None
    endereco_completo: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class DataCenterCreate(DataCenterBase):
    pass

class DataCenterSchema(DataCenterBase):
    id: int
    class Config:
        from_attributes = True

# --- SCHEMAS PARA BASTIDOR (RACK) ---

class BastidorBase(BaseModel):
    codigo_bast: str
    localizacao_id: int

class BastidorCreate(BastidorBase):
    pass

class BastidorSchema(BastidorBase):
    id: int
    class Config:
        from_attributes = True

# --- NOVO: Schemas para "Modelo de Equipamento" (EquipamentoTipo) ---
class EquipamentoTipoBase(BaseModel):
    nome: str
    modelo: Optional[str] = None
    tecnologia_equip: Optional[str] = None
    tipo_equip: Optional[str] = None
    estado_cv_equip: Optional[str] = None
    estado_op_equip: Optional[str] = None
    fabricante_id: int

class EquipamentoTipoCreate(EquipamentoTipoBase): pass
class EquipamentoTipoSchema(EquipamentoTipoBase):
    id: int
    class Config: from_attributes = True

# --- ATUALIZADO: Schema para criar uma "Instância de Equipamento" ---
class EquipamentoInstanciaCreate(BaseModel):
    nome_equip: str # Hostname
    equipamento_tipo_id: int
    bastidor_id: int
    codigo_rfid: str

class AdminAuth(AuthenticationBackend):
    async def login(self, request: StarletteRequest) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        # Credenciais definidas diretamente no código, como solicitado
        if username == "admin" and password == "wyrd":
            request.session.update({"token": "admin_logged_in"})
            return True
        return False

    async def logout(self, request: StarletteRequest) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: StarletteRequest) -> bool:
        return "token" in request.session

authentication_backend = AdminAuth(secret_key="W753y@r159d")

def seed_database():
    db = SessionLocal()
    try:
        # Garante que um andar padrão exista antes de criar os quartos
        default_andar_nome = "Andar Principal"
        andar_obj = db.query(Andar).filter(Andar.nome == default_andar_nome).first()
        if not andar_obj:
            andar_obj = Andar(nome=default_andar_nome)
            db.add(andar_obj)
            db.commit()
            db.refresh(andar_obj)

        num_quartos = db.query(Quarto).count()
        if num_quartos < NUM_FIXED_ROOMS:
            logger.info(f"INFO: Detectados {num_quartos}/{NUM_FIXED_ROOMS} quartos. Criando os restantes...")
            for i in range(num_quartos + 1, NUM_FIXED_ROOMS + 1):
                quarto_nome = f"Quarto {i}"
                if not db.query(Quarto).filter(Quarto.nome == quarto_nome).first():
                    # Associa o novo quarto ao andar padrão
                    db.add(Quarto(nome=quarto_nome, andar_id=andar_obj.id))
            db.commit()
            logger.info("INFO: Quartos fixos criados com sucesso.")
    except Exception as e:
        logger.error(f"ERRO ao 'semear' o banco de dados com quartos fixos: {e}")
        db.rollback()
    finally:
        db.close()

def seed_datacenters():
    db = SessionLocal()
    try:
        datacenters = ["Datacenter Principal SP", "Datacenter Secundário RJ", "Datacenter Sul"]
        for nome in datacenters:
            if not db.query(DataCenter).filter(DataCenter.nome == nome).first():
                # Gera um 'slug' simples a partir do nome
                slug = nome.lower().replace(" ", "-").replace("á", "a").replace("ç", "c")
                db_datacenter = DataCenter(nome=nome, slug=slug)
                db.add(db_datacenter)
        db.commit()
    except Exception as e:
        logger.error(f"ERRO ao 'semear' datacenters: {e}")
        db.rollback()
    finally:
        db.close()


#seed_database()
#seed_datacenters()

app = FastAPI(title="Wyrd-Baxter Connect")

admin = Admin(app, engine, authentication_backend=authentication_backend)

# Define como cada tabela será exibida no admin
class AssetAdmin(ModelView, model=Asset):
    column_list = [Asset.id, Asset.nome_ativo, Asset.mac_beacon, Asset.quarto]
    column_searchable_list = [Asset.nome_ativo, Asset.mac_beacon]
    name = "Ativo"
    name_plural = "Ativos"
    icon = "fa-solid fa-tag"

class EmbarcadoAdmin(ModelView, model=Embarcado):
    column_list = [Embarcado.id, Embarcado.id_esp, Embarcado.quarto]
    column_searchable_list = [Embarcado.id_esp]
    name = "Embarcado"
    name_plural = "Embarcados"
    icon = "fa-solid fa-microchip"

class QuartoAdmin(ModelView, model=Quarto):
    column_list = [Quarto.id, Quarto.nome]
    name = "Quarto"
    name_plural = "Quartos"
    icon = "fa-solid fa-door-closed"

class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    can_create = False
    can_edit = False
    column_list = [
        ReceivedEvent.id, ReceivedEvent.data_on, ReceivedEvent.ativo,
        ReceivedEvent.action, ReceivedEvent.status, ReceivedEvent.rssi
    ]
    column_searchable_list = [ReceivedEvent.ativo, ReceivedEvent.esp_id]
    column_sortable_list = [ReceivedEvent.id, ReceivedEvent.data_on]
    name = "Evento Recebido"
    name_plural = "Eventos Recebidos"
    icon = "fa-solid fa-list-ul"

class DataCenterAdmin(ModelView, model=DataCenter):
    name = "Localização (DC)"
    name_plural = "Localizações (DC)"
    icon = "fa-solid fa-map-location-dot"
    column_list = [DataCenter.id, DataCenter.nome, DataCenter.municipio, DataCenter.uf_abrv]
    form_columns = [
        DataCenter.nome, DataCenter.uf_abrv, DataCenter.estacao_abrv, DataCenter.edf,
        DataCenter.piso, DataCenter.sala, DataCenter.municipio, DataCenter.endereco_completo,
        DataCenter.latitude, DataCenter.longitude
    ]

class FabricanteAdmin(ModelView, model=Fabricante):
    name = "Fabricante"
    name_plural = "Fabricantes"
    icon = "fa-solid fa-industry"
    column_list = [Fabricante.id, Fabricante.nome]

class BastidorAdmin(ModelView, model=Bastidor):
    name = "Bastidor (Rack)"
    name_plural = "Bastidores (Racks)"
    icon = "fa-solid fa-server"
    column_list = [Bastidor.id, Bastidor.codigo_bast, Bastidor.localizacao]

# --- NOVO: View para gerenciar os Modelos de Equipamento ---
class EquipamentoTipoAdmin(ModelView, model=EquipamentoTipo):
    column_list = [EquipamentoTipo.id, EquipamentoTipo.nome, EquipamentoTipo.fabricante, EquipamentoTipo.modelo]
    name = "Modelo de Equipamento"
    name_plural = "Modelos de Equipamento"
    icon = "fa-solid fa-box-archive"

class EquipamentoAdmin(ModelView, model=Equipamento):
    name = "Instância de Equipamento"
    name_plural = "Instâncias de Equipamentos"
    icon = "fa-solid fa-hdd"
    column_list = [Equipamento.id, Equipamento.nome_equip, Equipamento.equipamento_tipo, Equipamento.bastidor, Equipamento.product]

class ProductAdmin(ModelView, model=Product):
    name = "Etiqueta RFID"
    name_plural = "Etiquetas RFID"
    icon = "fa-solid fa-tag"
    column_list = [Product.id, Product.codigo_rfid, Product.equipamento]
    column_searchable_list = [Product.codigo_rfid]

class InventorySnapshotAdmin(ModelView, model=InventorySnapshot):
    name = "Snapshot de Inventário"
    name_plural = "Snapshots de Inventário"
    icon = "fa-solid fa-camera"
    can_create = False # Geralmente criados pela aplicação
    can_edit = False
    column_list = [InventorySnapshot.id, InventorySnapshot.created_on, InventorySnapshot.datacenter]

class InventoryItemAdmin(ModelView, model=InventoryItem):
    name = "Item de Inventário"
    name_plural = "Itens de Inventário"
    icon = "fa-solid fa-tag"
    can_create = False
    can_edit = False
    column_list = [InventoryItem.id, InventoryItem.product, InventoryItem.snapshot]

admin.add_view(AssetAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(QuartoAdmin)
admin.add_view(ReceivedEventAdmin)

admin.add_view(DataCenterAdmin)
admin.add_view(FabricanteAdmin)
admin.add_view(BastidorAdmin)
admin.add_view(EquipamentoAdmin)
admin.add_view(EquipamentoTipoAdmin)
admin.add_view(ProductAdmin)
admin.add_view(InventorySnapshotAdmin)

# --- Dependência do Banco de Dados ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

# --- INÍCIO DA CORREÇÃO DE FUSO HORÁRIO ---
def to_sao_paulo_time(utc_dt: datetime):
    """Filtro Jinja2 para converter uma data UTC para o fuso de São Paulo (GMT-3)."""
    if not isinstance(utc_dt, datetime):
        return utc_dt # Retorna o valor original se não for uma data
    sao_paulo_tz = timezone(timedelta(hours=-3))
    return utc_dt.astimezone(sao_paulo_tz)

# Adiciona o filtro customizado ao ambiente do Jinja2 para que possamos usá-lo nos templates
templates.env.filters['to_spt'] = to_sao_paulo_time

def get_or_create_andar(db: Session, nome: str) -> Andar:
    andar = db.query(Andar).filter(Andar.nome == nome).first()
    if not andar:
        logger.info(f"Andar '{nome}' não encontrado. Criando novo registro.")
        andar = Andar(nome=nome)
        db.add(andar)
        db.commit()
        db.refresh(andar)
    return andar

def get_or_create_quarto(db: Session, nome: str, andar_id: int) -> Quarto:
    quarto = db.query(Quarto).filter(Quarto.nome == nome).first()
    if not quarto:
        logger.info(f"Quarto '{nome}' não encontrado. Criando e associando ao andar ID {andar_id}.")
        quarto = Quarto(nome=nome, andar_id=andar_id)
        db.add(quarto)
        db.commit()
        db.refresh(quarto)
    # Se o quarto já existe, mas pertence a outro andar (caso de edição)
    elif quarto.andar_id != andar_id:
        quarto.andar_id = andar_id
        db.commit()
    return quarto

@app.get("/login", name="login_page")
def display_login_page(request: Request):
    """
    Esta rota apenas exibe a página de login.
    """
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", name="login")
def handle_login(request: Request, username: str = Form(...), password: str = Form(...)):
    """
    Esta rota processa os dados do formulário de login.
    Por enquanto, ela apenas redireciona para a página de quartos.
    """
    # A lógica de autenticação pode ser adicionada aqui no futuro.
    # Por agora, qualquer login redireciona para a página de quartos.
    return RedirectResponse(url=request.url_for("list_quartos"), status_code=303)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    receiver_task = None
    pinger_task = None
    
    try:
        logger.info("[WebSocket] Nova conexão estabelecida. Cliente: %s, ID: %s", websocket.client, client_id)
        await websocket.send_text(json.dumps({"type": "CONNECTION_INFO", "client_id": client_id}))
        #logger.info("[WebSocket] ID '%s' enviado para o cliente.", client_id)

        async def receiver(ws: WebSocket):
            # Esta tarefa simplesmente espera. Se o cliente desconectar, ela irá falhar.
            async for _ in ws.iter_text():
                pass

        async def pinger(ws: WebSocket):
            while True:
                await asyncio.sleep(30)
                try:
                    # Tenta enviar o ping.
                    await ws.send_text("ping")
                    logger.debug("[WebSocket] Ping enviado para %s.", client_id)
                except (WebSocketException, RuntimeError):
                    # Se falhar porque a conexão está a fechar, quebra o loop silenciosamente.
                    # Isto é o que previne o erro.
                    break
        
        receiver_task = asyncio.create_task(receiver(websocket))
        pinger_task = asyncio.create_task(pinger(websocket))
        
        # Espera que uma das tarefas termine (o que indica uma desconexão)
        done, pending = await asyncio.wait(
            [receiver_task, pinger_task], return_when=asyncio.FIRST_COMPLETED
        )

    except Exception as e:
        logger.error("[WebSocket] Erro inesperado no endpoint com %s: %s", client_id, e, exc_info=True)
    finally:
        # Bloco de limpeza final
        if pinger_task: pinger_task.cancel()
        if receiver_task: receiver_task.cancel()
        
        manager.disconnect(client_id)
        logger.info("[WebSocket] Conexão com o cliente %s limpa e encerrada.", client_id)


scanning_sessions = {} # Dicionário para controlar as tarefas de scan de cada cliente

scanning_tasks = {} # Dicionário para controlar as tarefas de scan de cada cliente

@app.websocket("/ws/rfid")
async def rfid_websocket_endpoint(websocket: WebSocket, db: Session = Depends(get_db)):
    client_id = f"{websocket.client.host}:{websocket.client.port}"
    await websocket.accept()
    logger.info(f"Cliente RFID conectado: {client_id}")
    
    try:
        while True:
            data_text = await websocket.receive_text()
            data = json.loads(data_text)
            action = data.get("action")

            if action == "start_scan":
                if client_id in scanning_tasks and not scanning_tasks[client_id].done():
                    await websocket.send_text(json.dumps({"type": "scan_error", "message": "Um scan já está em progresso."}))
                    continue
                
                datacenter_id = data.get("datacenter_id")
                if not datacenter_id:
                    await websocket.send_text(json.dumps({"type": "scan_error", "message": "Selecione um datacenter."}))
                    continue
                task = asyncio.create_task(scan_rfid.rfid_scan_task(websocket, mode='multiple', datacenter_id=datacenter_id))
                scanning_tasks[client_id] = task

            elif action == "read_single_tag": # Leitura única
                task = asyncio.create_task(scan_rfid.rfid_scan_task(websocket, mode='single'))
                scanning_tasks[client_id] = task
                
            elif action == "stop_scan":
                if client_id in scanning_tasks and not scanning_tasks[client_id].done():
                    task = scanning_tasks[client_id]
                    task.cancel() # Envia o sinal para a tarefa parar
                    
                    tags_lidas = []
                    try:
                        # Aguarda a tarefa ser cancelada e recolhe a lista de tags
                        tags_lidas = await task 
                    except asyncio.CancelledError:
                        logger.info(f"Scan para o cliente {client_id} foi cancelado via botão 'parar'.")
                        # O bloco finally na tarefa fará a limpeza
                        pass

                    # Após a finalização da tarefa, salva os resultados e limpa o estado
                    if scan_rfid.save_tags_as_inventory(db, data.get("datacenter_id"), tags_lidas):
                        await manager.broadcast("ATUALIZAR_ESTADO") # Notifica a todos para recarregar
                    
                    # Remove a tarefa finalizada do dicionário de controle
                    del scanning_tasks[client_id]

            elif action == "cancel_scan":
                if client_id in scanning_tasks and not scanning_tasks[client_id].done():
                    logger.info(f"Scan para o cliente {client_id} foi cancelado via botão 'cancelar'.")
                    task = scanning_tasks[client_id]
                    task.cancel() # Apenas cancela a tarefa, sem salvar nada
                    del scanning_tasks[client_id]
                # Envia uma confirmação para o frontend resetar a UI
                await websocket.send_text(json.dumps({"type": "scan_status", "message": "Leitura cancelada."}))

    except WebSocketDisconnect:
        # Se o usuário fechar a página, cancela e limpa a tarefa de scan associada
        if client_id in scanning_tasks and scanning_tasks[client_id]:
            scanning_tasks[client_id].cancel()
            del scanning_tasks[client_id]
        logger.info(f"Cliente RFID {client_id} desconectado. Scan (se houver) interrompido.")

# ===================================================================
# SEÇÃO 1: ROTAS DE ALTO NÍVEL, CONFIGURAÇÕES E API PARA ESPs
# ===================================================================

@app.get("/api/time", name="get_server_time")
def get_server_time():
    """
    Endpoint para que os ESPs possam sincronizar seu relógio.
    Retorna o tempo atual do servidor como um timestamp Unix (segundos desde 1970).
    """
    return {"unix_time": int(time.time())}

@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("login_page"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reset", name="reset_esp_state")
async def reset_esp_state(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    await synchronize_and_reset_esp(db=db, embarcado_id=embarcado_id)
    time.sleep(1) 
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/test_rssi", name="test_rssi_esp")
async def test_rssi_esp(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    embarcado_id = data.get("embarcado_id")
    client_id = data.get("client_id")

    if not embarcado_id or not client_id:
        raise HTTPException(status_code=400, detail="embarcado_id e client_id são necessários.")

    embarcado = db.query(Embarcado).get(embarcado_id)
    if not embarcado:
        raise HTTPException(status_code=404, detail="Embarcado não encontrado.")

    logger.info("Gerando relatório RSSI para a ESP '%s' a pedido do cliente '%s'.", embarcado.id_esp, client_id)
    
    report_data = []
    
    for mac, state in _asset_realtime_state.items():
        if embarcado.id_esp in state.readings:
            reading = state.readings[embarcado.id_esp]
            report_data.append({
                "mac": mac,
                "rssi": reading.get("rssi", -1000) 
            })
    
    websocket_message = {
        "type": "RSSI_REPORT",
        "esp_id": embarcado.id_esp,
        "report": report_data
    }

    await manager.send_to_client(client_id, json.dumps(websocket_message))
    
    return Response(status_code=status.HTTP_200_OK)

@app.get("/api/assets/map", name="get_assets_map")
def get_assets_map(db: Session = Depends(get_db)):
    """
    Retorna um dicionário JSON simples mapeando
    o mac_beacon de cada ativo para o seu nome_ativo.
    """
    assets = db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()
    asset_map = {asset.mac_beacon: asset.nome_ativo for asset in assets}
    return asset_map

@app.post("/embarcados/{embarcado_id}/reconfigure", name="reconfigure_esp")
def reconfigure_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        logger.info("Enviando comando 'FETCH_CONFIG' individual para a ESP '%s'.", embarcado.id_esp)        
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
        
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/embarcados/{embarcado_id}/reboot", name="reboot_esp")
def reboot_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        logger.info("Enviando comando 'REBOOT' para a ESP '%s'.", embarcado.id_esp)
        command = {"type": "command", "data": {"name": "REBOOT"}}
        # Usamos a função que envia para o canal individual da ESP
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/settings/update", name="update_settings")
def update_settings(
    request: Request, db: Session = Depends(get_db), 
    rssi_threshold: str = Form(...),
    conflict_margin_db: str = Form(...),
    inercia_entrada: str = Form(...), # Novo
    inercia_saida: str = Form(...)   # Novo
):
    settings_data = {
        "rssi_threshold": rssi_threshold,
        "conflict_margin_db": conflict_margin_db,
        "inercia_entrada": inercia_entrada,
        "inercia_saida": inercia_saida,
    }
    for key, value in settings_data.items():
        setting = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
        if not setting:
            setting = GlobalSetting(key=key)
            db.add(setting)
        setting.value = value
    db.commit()
    aggregator.flag_for_reload()
    logger.info(f"[main] Configurações globais do motor RTLS salvas: {settings_data}")
    # Já não é preciso enviar comando para as ESPs, o servidor agora gere isto.
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

LIVENESS_CHECK_INTERVAL_SEC = 30
FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))
ESP_TIMEOUT_SEC = 150 # 2.5 minutos (2.5 * 60)
ESP_STATUS_UPDATE_INTERVAL_SEC = 90 

async def check_esp_liveness():
    """
    Tarefa de background que verifica na base de dados por ESPs offline.
    Usa o campo 'status_rede' para um estado de quarentena persistente.
    """
    while True:
        await asyncio.sleep(LIVENESS_CHECK_INTERVAL_SEC)
        
        db = SessionLocal()
        try:
            now_utc = datetime.now(timezone.utc)
            cutoff_time = now_utc - timedelta(seconds=ESP_TIMEOUT_SEC)
            
            # 1. A query agora busca apenas os ESPs que o sistema considera 'online'.
            #    Isto substitui a verificação do set '_esps_em_quarentena'.
            esps_a_verificar = db.query(Embarcado).filter(
                Embarcado.last_seen != None,
                Embarcado.status_rede == 'online'
            ).all()

            esps_que_ficaram_offline = []
            for emb in esps_a_verificar:
                # A lógica de verificação do tempo é a mesma
                last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
                if last_seen_utc < cutoff_time:
                    esps_que_ficaram_offline.append(emb)
            
            if esps_que_ficaram_offline:
                logger.warning("[LIVENESS] ESPs considerados offline nesta verificação: %s", [e.id_esp for e in esps_que_ficaram_offline])
                
                for emb in esps_que_ficaram_offline:
                    await release_assets_for_offline_esp(db, emb.id_esp)
                    
                    emb.status_rede = 'offline'
                    emb.wifi_signal = None  
                    emb.mac_address = None 
                    emb.ip_address = None
                
                # 3. Commit final para salvar todas as alterações de status na base de dados.
                db.commit()
        
        except Exception as e:
            logger.error("[LIVENESS] Ocorreu um erro durante a verificação de atividade das ESPs: %s", e, exc_info=True)
            db.rollback()
        finally:
            db.close()

async def batch_update_esp_status():
    """
    Tarefa de background que periodicamente escreve o status mais recente
    dos ESPs (last_seen, wifi_signal) no banco de dados de uma só vez.
    """
    # Adicione uma constante no topo do seu main.py, se não existir
    # ESP_STATUS_UPDATE_INTERVAL_SEC = 90 
    
    logger.info("[BATCH-UPDATE-ESP] Serviço de atualização de status de embarcados iniciado.")
    while True:
        await asyncio.sleep(90) # Roda a cada 90 segundos
        
        status_updates = mqtt_client.get_and_clear_status_cache()
        if not status_updates:
            continue

        logger.info(f"[BATCH-UPDATE-ESP] Atualizando status de {len(status_updates)} embarcados no banco de dados.")
        db = SessionLocal()
        try:
            esp_ids_to_update = list(status_updates.keys())
            embarcados_to_update = db.query(Embarcado).filter(Embarcado.id_esp.in_(esp_ids_to_update)).all()
            
            for emb in embarcados_to_update:
                if emb.id_esp in status_updates:
                    data = status_updates[emb.id_esp]
                    emb.last_seen = data["last_seen"]
                    if "wifi_signal" in data:
                        emb.wifi_signal = data["wifi_signal"]
                    if emb.status_rede == 'offline':
                        emb.status_rede = 'online'
            
            db.commit()
        except Exception as e:
            logger.error(f"[BATCH-UPDATE-ESP] Erro ao atualizar status dos embarcados: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

def get_global_settings(db: Session) -> dict:
    settings_from_db = db.query(GlobalSetting).all()
    defaults = {"rssi_threshold": "-60", "inercia_chegada": "500", "inercia_saida": "15000"}
    db_settings = {s.key: s.value for s in settings_from_db}
    return {**defaults, **db_settings}

@app.get("/api/esp/handshake", name="esp_handshake")
def esp_handshake(
    request: Request,
    db: Session = Depends(get_db),
    id_esp: str = Query(...),
    mac: str = Query(...),
    ip: str = Query("N/A"),
    fw: str = Query("N/A")
):
    logger.info(f"HANDSHAKE recebido da ESP: {id_esp} (MAC: {mac}, IP: {ip}, FW: {fw})")

    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    if embarcado:
        # Lógica para salvar o MAC e IP
        embarcado.mac_address = mac
        embarcado.ip_address = ip
        db.commit()
    else:
        logger.warning(f"Handshake recebido de um embarcado não cadastrado: {id_esp}")

    all_assets = db.query(Asset.mac_beacon).filter(Asset.mac_beacon.isnot(None)).all()
    whitelist = [m for m, in all_assets]

    logger.info(f"Enviando configuração para {id_esp}: {len(whitelist)} ativos na whitelist.")
    return {"whitelist": whitelist}

@app.get("/planta", name="view_planta")
def view_planta(request: Request, db: Session = Depends(get_db)):
    """
    Renderiza a página da planta baixa interativa.
    """
    return templates.TemplateResponse("planta_baixa.html", {"request": request})

@app.get("/api/planta/dados", name="get_planta_dados")
def get_planta_dados(db: Session = Depends(get_db)):
    """
    Endpoint de API que fornece os dados de ocupação dos quartos,
    incluindo o status do embarcado e detalhes de cada ativo.
    """
    quartos = db.query(Quarto).options(
        joinedload(Quarto.assets),
        joinedload(Quarto.embarcados)
    ).order_by(Quarto.id).all()
    
    dados_quartos = []
    now_utc = datetime.now(timezone.utc)
    sao_paulo_tz = timezone(timedelta(hours=-3))

    for quarto in quartos:
        status_embarcado = "Offline"
        if quarto.embarcados: # Verifica se existe um embarcado associado
            embarcado = quarto.embarcados[0] # Pega o primeiro (deve ser apenas um)
            if embarcado.last_seen:
                last_seen_utc = embarcado.last_seen.replace(tzinfo=timezone.utc)
                if (now_utc - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                    status_embarcado = "Online"
        
        # 2. Obter detalhes de cada ativo individualmente
        ativos_detalhados = []
        for asset in quarto.assets:
            # Para cada ativo, busca o seu último evento de entrada bem sucedido
            ultimo_evento = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status.in_(['OK', 'Confirmado'])
            ).order_by(desc(ReceivedEvent.data_on)).first()
            
            horario = "N/A"
            if ultimo_evento:
                # Usamos um fuso horário para formatar a hora local corretamente
                horario_local = ultimo_evento.data_on.astimezone(sao_paulo_tz)
                horario = horario_local.strftime("%H:%M:%S")
            ativos_detalhados.append({
                "nome": asset.nome_ativo,
                "horario_entrada": horario
            })

        dados_quartos.append({
            "id_quarto": f"quarto-{quarto.id}",
            "nome_quarto": quarto.nome,
            "numero_ativos": len(quarto.assets),
            "status_embarcado": status_embarcado, # <-- NOVO DADO
            "ativos": ativos_detalhados          # <-- NOVA ESTRUTURA DE DADOS
        })
        
    return JSONResponse(content=dados_quartos)

# ===================================================================
# SEÇÃO 2: CRUD PARA QUARTOS
# ===================================================================
@app.get("/quartos", name="list_quartos")
def list_quartos(request: Request, db: Session = Depends(get_db)):
    """
    Exibe o dashboard de status dos quartos, agora carregando também os andares.
    """
    # A query foi atualizada para carregar o andar junto com o quarto e os ativos
    quartos_com_assets = db.query(Quarto).options(
        joinedload(Quarto.assets),
        joinedload(Quarto.andar)  # <-- MUDANÇA IMPORTANTE AQUI
    ).order_by(Quarto.id).all()

    # O resto da lógica para encontrar a data de entrada dos ativos permanece
    for quarto in quartos_com_assets:
        for asset in quarto.assets:
            ultimo_evento_entrada = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status.in_(['OK', 'Confirmado'])
            ).order_by(ReceivedEvent.data_on.desc()).first()

            if ultimo_evento_entrada:
                asset.data_entrada_obj = ultimo_evento_entrada.data_on
                asset.data_entrada_str = ultimo_evento_entrada.data_on.strftime("%d/%m/%Y às %H:%M:%S")
            else:
                asset.data_entrada_obj = datetime.min.replace(tzinfo=timezone.utc)
                asset.data_entrada_str = "Horário de entrada não registrado"
        
        quarto.assets.sort(key=lambda b: b.data_entrada_obj)

    return templates.TemplateResponse("quartos_list.html", {
        "request": request,
        "quartos": quartos_com_assets
    })

@app.post("/quartos/{quarto_id}/edit", name="update_quarto")
def update_quarto(request: Request, quarto_id: int, nome: str = Form(...), db: Session = Depends(get_db)):
    """
    Processa a atualização do nome de um quarto (submetido pelo formulário inline).
    """
    quarto = db.query(Quarto).get(quarto_id)
    if quarto:
        quarto.nome = nome
        db.commit()
        aggregator.flag_for_reload() 
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)


# ===================================================================
# SEÇÃO 3: CRUD PARA EMBARCADOS
# ===================================================================
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(
    request: Request, db: Session = Depends(get_db),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("id_esp"),
    order: Optional[str] = Query("asc")
):
    # A query agora precisa carregar o andar junto com o quarto
    query = db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar))
    
    # Lógica de busca
    if search:
        search_term = f"%{search}%"
        query = query.join(Embarcado.quarto).join(Quarto.andar).filter(
            or_(Embarcado.id_esp.ilike(search_term), Quarto.nome.ilike(search_term), Andar.nome.ilike(search_term), Embarcado.mac_address.ilike(search_term), Embarcado.ip_address.ilike(search_term))
        )
    
    # Lógica de ordenação
    sortable_columns = {
        "id_esp": Embarcado.id_esp, "andar": Andar.nome, "quarto": Quarto.nome,
        "status": Embarcado.status_rede, "wifi_signal": Embarcado.wifi_signal, "rssi_min": Embarcado.rssi_threshold,
        "mac_address": Embarcado.mac_address, "ip_address": Embarcado.ip_address
    }
    if sort_by in ["andar", "quarto"]:
        query = query.join(Embarcado.quarto).join(Quarto.andar)

    sort_column = sortable_columns.get(sort_by, Embarcado.id_esp)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))
    
    embarcados = query.all()
    
    global_settings = get_global_settings(db)
    rssi_thresholds = {
        "global": int(global_settings.get("rssi_threshold", -60)),
        "individuais": {
            emb.id_esp: emb.rssi_threshold for emb in embarcados if emb.rssi_threshold is not None
        }
    }
    
    # Lógica para formatar a data (last_seen)
    fuso_local = timezone(timedelta(hours=-3))
    for emb in embarcados:
        emb.status = emb.status_rede.capitalize() if emb.status_rede else "Desconhecido"
        if emb.last_seen:
            last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
            data_local = last_seen_utc.astimezone(fuso_local)
            emb.last_seen_str = data_local.strftime("às %H:%M:%S de %d/%m")
        else:
            emb.last_seen_str = "Nunca visto"

    assigned_quarto_ids = {emb.quarto_id for emb in db.query(Embarcado).filter(Embarcado.quarto_id.isnot(None)).all()}
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()
    
    # A LINHA MAIS IMPORTANTE: enviando a variável que faltava
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "available_quartos": available_quartos,
        "form_action": request.url_for("create_embarcado"),
        "embarcado": None, 
        "search": search,
        "global_settings": get_global_settings(db),
        "rssi_thresholds": json.dumps(rssi_thresholds),
        "current_filters": {"search": search, "sort_by": sort_by, "order": order}
    })

@app.post("/embarcados/new", name="create_embarcado")
def create_embarcado(
    request: Request,
    id_esp: str = Form(...),
    andar_nome: str = Form(...),
    quarto_nome: str = Form(...),
    rssi_threshold: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    andar_obj = get_or_create_andar(db, andar_nome.strip())
    quarto_obj = get_or_create_quarto(db, quarto_nome.strip(), andar_obj.id)
    
    rssi_value = int(rssi_threshold) if rssi_threshold else None
    
    novo_embarcado = Embarcado(id_esp=id_esp, quarto_id=quarto_obj.id, rssi_threshold=rssi_value)
        
    try:
        db.add(novo_embarcado)
        db.commit()
        db.refresh(novo_embarcado)
        aggregator.flag_for_reload()
        logger.info(f"[main] Embarcado '{novo_embarcado.id_esp}' criado. Disparando reset automático.")
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=novo_embarcado.id_esp, command=command)
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar embarcado: {e}")

    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb_para_editar = db.query(Embarcado).get(embarcado_id)
    
    assigned_quarto_ids = {
        emb.quarto_id for emb in db.query(Embarcado).filter(
            Embarcado.id != embarcado_id,
            Embarcado.quarto_id.isnot(None)
        ).all()
    }
    
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()
    
    # Adicionando a variável que faltava
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar)).order_by(Embarcado.id_esp).all(),
        "available_quartos": available_quartos,
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": emb_para_editar,
        "search": None, 
        "global_settings": get_global_settings(db),
        "rssi_thresholds": json.dumps({ "global": 0, "individuais": {} }),
        "current_filters": {"search": None, "sort_by": "id_esp", "order": "asc"} # <-- A CORREÇÃO ESTÁ AQUI
    })

# Em main.py

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(
    request: Request,
    embarcado_id: int,
    andar_nome: str = Form(...),
    quarto_nome: str = Form(...),
    rssi_threshold: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        andar_obj = get_or_create_andar(db, andar_nome.strip())
        quarto_obj = get_or_create_quarto(db, quarto_nome.strip(), andar_obj.id)
        
        rssi_value = int(rssi_threshold) if rssi_threshold else None
        
        emb.quarto_id = quarto_obj.id
        emb.rssi_threshold = rssi_value
        db.commit()
        aggregator.flag_for_reload()
        logger.info(f"[main] Embarcado '{emb.id_esp}' atualizado. Disparando reset automático.")
        command = {"type": "command", "data": {"name": "FETCH_CONFIG"}} 
        mqtt_client.publish_command_to_esp(esp_id=emb.id_esp, command=command)
        
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{embarcado_id}/delete", name="delete_embarcado")
def delete_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        db.delete(emb)
        db.commit()
        aggregator.flag_for_reload() 

    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


# ===================================================================
# SEÇÃO 4: CRUD PARA ATIVOS
# ===================================================================
@app.get("/assets", name="list_assets")
def list_assets(
    request: Request, db: Session = Depends(get_db),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("nome_ativo"),
    order: Optional[str] = Query("asc")
):
    query = db.query(Asset).options(joinedload(Asset.quarto))
    
    # Lógica de busca
    if search:
        search_term = f"%{search}%"
        # O outerjoin é usado para que a busca funcione mesmo em ativos que não estão em nenhum quarto
        query = query.outerjoin(Asset.quarto).filter(
            or_(Asset.nome_ativo.ilike(search_term), 
                Asset.mac_beacon.ilike(search_term), 
                Quarto.nome.ilike(search_term), 
                Asset.tipo_ativo.ilike(search_term))
        )

    # Lógica de ordenação
    sortable_columns = {
        "nome_ativo": Asset.nome_ativo, "tipo_ativo": Asset.tipo_ativo,
        "mac_beacon": Asset.mac_beacon, 
        "quarto": Quarto.nome
    }
    if sort_by == "quarto":
        query = query.outerjoin(Asset.quarto)
        
    sort_column = sortable_columns.get(sort_by, Asset.nome_ativo)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    assets = query.all()
    
    # Enviando a variável que faltava para o template
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": assets,
        "form_action": request.url_for("create_asset"), "asset": None, 
        "current_filters": {"search": search, "sort_by": sort_by, "order": order}
    })

@app.post("/assets", name="create_asset")
def create_asset(
    request: Request,
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    tipo_ativo: str = Form(None),
    db: Session = Depends(get_db)
):
    asset = Asset(
        nome_ativo=nome_ativo,
        mac_beacon=mac_beacon.lower(),
        tipo_ativo=tipo_ativo
    )
    try:
        db.add(asset)
        db.commit()
        aggregator.flag_for_reload()
        logger.info("[main] Ativo criado. Enviando comando de sincronização para todas as ESPs.")
        command_payload = {"command": "fetch_config"}
        mqtt_client.client.publish(topic=settings.get("mqtt_esp_command_topic"), payload=json.dumps(command_payload), qos=1)
    except IntegrityError:
        db.rollback()
        logger.error(f"[main-db] ERRO: Tentativa de criar ativo com nome ou MAC duplicado: {nome_ativo} / {mac_beacon.lower()}")
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar ativo: {e}")
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/assets/{asset_id}/edit", name="edit_asset")
def edit_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    # A única mudança é adicionar o "current_filters" no dicionário
    return templates.TemplateResponse("assets_list.html", {
        "request": request, 
        "assets": db.query(Asset).order_by(Asset.nome_ativo).all(),
        "form_action": request.url_for("update_asset", asset_id=asset_id),
        "asset": db.query(Asset).get(asset_id), 
        "search": None,
        "current_filters": {"search": None, "sort_by": "nome_ativo", "order": "asc"} # <-- A CORREÇÃO ESTÁ AQUI
    })

@app.post("/assets/{asset_id}/edit", name="update_asset")
def update_asset(
    request: Request,
    asset_id: int,
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    tipo_ativo: str = Form(None),
    db: Session = Depends(get_db)
):
    asset = db.query(Asset).get(asset_id)
    if asset:
        asset.nome_ativo = nome_ativo
        asset.mac_beacon = mac_beacon.lower()
        asset.tipo_ativo = tipo_ativo
        
        db.commit()
        aggregator.flag_for_reload() 
        logger.info("[main] Ativo atualizado. Enviando comando de sincronização para todas as ESPs.")
        command_payload = {"command": "fetch_config"}
        mqtt_client.client.publish(topic=settings.get("mqtt_esp_command_topic"), payload=json.dumps(command_payload), qos=1)
            
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/assets/{asset_id}/delete", name="delete_asset")
def delete_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).get(asset_id)
    if asset:
        db.delete(asset)
        db.commit()
        aggregator.flag_for_reload()
        logger.info("[main] Ativo apagado. Enviando comando de sincronização para todas as ESPs.")
        command_payload = {"command": "fetch_config"}
        mqtt_client.client.publish(topic=settings.get("mqtt_esp_command_topic"), payload=json.dumps(command_payload), qos=1)
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

# ===================================================================
# SEÇÃO 5: HISTÓRICO DE EVENTOS E DOWNLOADS
# ===================================================================
@app.get("/events", name="list_events")
def list_events(
    request: Request, page: int = Query(1, ge=1),
    filter_ativo: Optional[str] = Query(None), filter_quarto: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), filter_status: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None), db: Session = Depends(get_db)
):
    # O mapa de embarcados já não é necessário aqui, a lógica fica mais simples
    asset_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)
    
    # A lógica de filtro por quarto agora funciona com o novo campo!
    if filter_quarto: 
        query = query.filter(ReceivedEvent.quarto_nome == filter_quarto)
    
    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if filter_action: query = query.filter(ReceivedEvent.action == filter_action)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))

    total = query.count()
    events = query.order_by(ReceivedEvent.data_on.desc()).offset((page - 1) * EVENT_PAGE_SIZE).limit(EVENT_PAGE_SIZE).all()

    sao_paulo_tz = timezone(timedelta(hours=-3))
    for e in events:
        e.nome_ativo = asset_map.get(e.ativo, e.ativo)
        
        e.quarto = e.quarto_nome if e.quarto_nome else "N/A"

        data_utc = e.data_on.replace(tzinfo=timezone.utc)
        data_local = data_utc.astimezone(sao_paulo_tz)
        e.data_str = data_local.strftime("%d/%m/%Y")
        e.hora_str = data_local.strftime("%H:%M:%S")

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": total > page * EVENT_PAGE_SIZE,
        "all_assets": db.query(Asset.nome_ativo, Asset.mac_beacon).distinct().order_by(Asset.nome_ativo).all(),
        "all_action_options": [("GET", "Conectar"), ("OUT", "Desconectar")],
        "all_status_options": ["OK", "Erro", "Enfileirado", "Ignorado", "Confirmado"],
        "all_quartos": sorted([q.nome for q in db.query(Quarto).order_by(Quarto.nome).all()]),
        "current_filters": {"ativo": filter_ativo, "quarto": filter_quarto, "action": filter_action, "status": filter_status, "time_filter": time_filter}
    })

@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    db: Session = Depends(get_db),
    # Parâmetros de filtro, agora incluindo a AÇÃO
    filter_ativo: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), # <-- PARÂMETRO ADICIONADO
    time_filter: Optional[str] = Query(None)
):
    embarcados_map = {emb.id_esp: emb.quarto.nome for emb in db.query(Embarcado).options(joinedload(Embarcado.quarto)).all() if emb.quarto}
    beacon_to_asset_name_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)

    # Aplica todos os mesmos filtros da página de eventos
    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
    if filter_quarto:
        esps_ids = [id for id, nome in embarcados_map.items() if nome == filter_quarto]
        query = query.filter(ReceivedEvent.esp_id.in_(esps_ids)) if esps_ids else query.filter(False)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    
    # --- LÓGICA DE FILTRO ADICIONADA AQUI ---
    if filter_action:
        query = query.filter(ReceivedEvent.action == filter_action)

    events = query.order_by(ReceivedEvent.data_on.desc()).all()

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Data/Hora", "Nome do Ativo", "Quarto", "Status", "Ação", "RSSI"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        action_map = {"GET": "Conectar", "OUT": "Desconectar"}

        for e in events:
            quarto = embarcados_map.get(e.esp_id, "Desconhecido")
            nome_ativo = beacon_to_asset_name_map.get(e.ativo, e.ativo)
            acao_traduzida = action_map.get(e.action, e.action)
            writer.writerow([
                e.data_on.strftime("%Y-%m-%d %H:%M:%S") if e.data_on else "",
                nome_ativo, quarto, e.status, acao_traduzida, e.rssi
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

@app.get("/assets/download", name="download_assets_csv")
def download_assets_csv(db: Session = Depends(get_db)):
    # --- CORREÇÃO AQUI: Usa 'joinedload' para carregar o quarto junto ---
    assets = db.query(Asset).options(joinedload(Asset.quarto)).order_by(Asset.nome_ativo).all()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["NOME DO ATIVO", "MAC BEACON", "QUARTO ATUAL"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for asset in assets:
            # --- CORREÇÃO AQUI: Acessa o nome do quarto de forma segura ---
            quarto_nome = asset.quarto.nome if asset.quarto else ""
            writer.writerow([asset.nome_ativo, asset.mac_beacon, quarto_nome])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ativos_export.csv"}
    )

@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    # --- CORREÇÃO AQUI: Usa 'joinedload' para carregar o quarto junto ---
    embarcados = db.query(Embarcado).options(joinedload(Embarcado.quarto)).order_by(Embarcado.id_esp).all()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        # --- CORREÇÃO AQUI: Remove a coluna 'ANDAR' que não existe mais ---
        writer.writerow(["ID DO EMBARCADO", "QUARTO"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for emb in embarcados:
            # --- CORREÇÃO AQUI: Acessa o nome do quarto e remove 'andar' ---
            quarto_nome = emb.quarto.nome if emb.quarto else ""
            writer.writerow([emb.id_esp, quarto_nome])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"}
    )

# ===================================================================
# SEÇÃO 5.1: DATACENTERS
# ===================================================================7
# Schema Pydantic para as respostas da API
class DatacenterSchema(BaseModel):
    id: int
    nome: str
    slug: str
    snapshots_count: int

    class Config:
        from_attributes = True

# Schema para validação na criação
class DatacenterCreate(BaseModel):
    nome: str

# Schema para validação na atualização
class DatacenterUpdate(BaseModel):
    nome: str

# ROTA PARA LISTAR TODOS OS DATACENTERS (para o modal de gerenciamento)
@app.get("/api/datacenters", response_model=List[DatacenterSchema], name="list_datacenters_api")
def list_datacenters_api(db: Session = Depends(get_db)):
    """Retorna uma lista de todos os datacenters, incluindo a contagem de snapshots."""
    datacenters = db.query(DataCenter).order_by(DataCenter.nome).all()
    for dc in datacenters:
        dc.snapshots_count = db.query(InventorySnapshot).filter(InventorySnapshot.datacenter_id == dc.id).count()
    return datacenters

# ROTA PARA CRIAR UM NOVO DATACENTER
@app.post("/api/datacenters/new", response_model=DatacenterSchema, name="create_datacenter_api")
def create_datacenter_api(
    dc_in: DatacenterCreate,
    db: Session = Depends(get_db)
):
    """Cria um novo datacenter."""
    if db.query(DataCenter).filter(DataCenter.nome == dc_in.nome).first():
        raise HTTPException(status_code=409, detail="Um datacenter com este nome já existe.")
    slug = dc_in.nome.lower().replace(" ", "-").replace("á", "a").replace("ç", "c")
    novo_dc = DataCenter(nome=dc_in.nome, slug=slug)
    db.add(novo_dc)
    db.commit()
    db.refresh(novo_dc)
    novo_dc.snapshots_count = 0 # Define a contagem inicial como 0
    logger.info(f"Novo datacenter criado via API: {novo_dc.nome}")
    return novo_dc

# ROTA PARA ATUALIZAR (EDITAR) UM DATACENTER
@app.put("/api/datacenters/{dc_id}", response_model=DatacenterSchema, name="update_datacenter_api")
def update_datacenter_api(
    dc_id: int,
    dc_in: DatacenterUpdate,
    db: Session = Depends(get_db)
):
    """Atualiza o nome de um datacenter existente."""
    dc_to_update = db.query(DataCenter).get(dc_id)
    if not dc_to_update:
        raise HTTPException(status_code=404, detail="Datacenter não encontrado.")
    
    dc_to_update.nome = dc_in.nome
    dc_to_update.slug = dc_in.nome.lower().replace(" ", "-").replace("á", "a").replace("ç", "c")
    db.commit()
    db.refresh(dc_to_update)
    dc_to_update.snapshots_count = db.query(InventorySnapshot).filter(InventorySnapshot.datacenter_id == dc_to_update.id).count()
    return dc_to_update

# ROTA PARA DELETAR UM DATACENTER
@app.delete("/api/datacenters/{dc_id}", name="delete_datacenter_api")
def delete_datacenter_api(dc_id: int, db: Session = Depends(get_db)):
    """Deleta um datacenter e TODOS os seus inventários associados."""
    dc_to_delete = db.query(DataCenter).get(dc_id)
    if not dc_to_delete:
        raise HTTPException(status_code=404, detail="Datacenter não encontrado.")
    
    # Apaga todos os snapshots associados primeiro
    db.query(InventorySnapshot).filter(InventorySnapshot.datacenter_id == dc_id).delete(synchronize_session=False)

    # Agora, apaga o datacenter
    db.delete(dc_to_delete)
    db.commit()
    return {"ok": True, "detail": "Datacenter e seus inventários associados foram excluídos."}


# ===================================================================
# SEÇÃO 5.2: INVENTÁRIO DE PRODUTOS
# ===================================================================

@app.get("/inventario", name="list_inventario")
def inventory_page(
    request: Request,
    db: Session = Depends(get_db),
    view: str = Query("current"),
    datacenter_id: Optional[int] = Query(None)
):
    all_datacenters = db.query(DataCenter).order_by(DataCenter.id).all()

    if not datacenter_id and all_datacenters:
        primeiro_dc_id = all_datacenters[0].id
        params = dict(request.query_params)
        params['datacenter_id'] = primeiro_dc_id
        redirect_url = request.url.replace(query=urlencode(params))
        return RedirectResponse(url=str(redirect_url))

    # --- NOVA LÓGICA DE DECISÃO ---
    snapshot_query = db.query(InventorySnapshot).filter(InventorySnapshot.datacenter_id == datacenter_id)
    snapshot_count = snapshot_query.count()
    
    snapshots = snapshot_query.order_by(InventorySnapshot.created_on.desc()).limit(2).all()
    snapshot_atual = snapshots[0] if snapshots else None
    snapshot_anterior = snapshots[1] if len(snapshots) > 1 else None

    # Variáveis para o template
    inventario_atual_formatado = []
    tabela_unificada = []
    itens_encontrados, itens_faltando, itens_novos = [], [], []
    comparison_mode = None # Informa ao template qual relatório mostrar

    # --- Lógica da Visão "CURRENT" ---
    if view == "current" and snapshot_atual:
        # (Esta parte não muda)
        itens_query = db.query(Product.codigo_rfid, Equipamento.nome_equip)\
                        .join(InventoryItem, InventoryItem.product_id == Product.id)\
                        .outerjoin(Product.equipamento)\
                        .filter(InventoryItem.snapshot_id == snapshot_atual.id)\
                        .order_by(Product.codigo_rfid).all()
        for codigo, nome_equip in itens_query:
            inventario_atual_formatado.append({ "codigo_rfid": codigo, "nome_equip": nome_equip or "Não associado", "created_on": snapshot_atual.created_on })

    # --- Lógica da Visão "COMPARISON" ---
    elif view == "comparison":
        # Cenário 1: Primeiro inventário -> Compara com o Catálogo Mestre
        if snapshot_count <= 1 and snapshot_atual:
            comparison_mode = "master"
            # (Lógica de Reconciliação Mestra que já fizemos)
            query_catalogo = db.query(Product.codigo_rfid, Equipamento.nome_equip)\
                               .join(Equipamento, Product.equipamento_id == Equipamento.id)\
                               .join(Bastidor, Equipamento.bastidor_id == Bastidor.id)\
                               .filter(Bastidor.localizacao_id == datacenter_id).all()
            mapa_catalogo = {codigo: nome for codigo, nome in query_catalogo}
            set_catalogo = set(mapa_catalogo.keys())

            query_snapshot = db.query(Product.codigo_rfid, Equipamento.nome_equip)\
                               .join(InventoryItem, InventoryItem.product_id == Product.id)\
                               .outerjoin(Product.equipamento)\
                               .filter(InventoryItem.snapshot_id == snapshot_atual.id).all()
            mapa_snapshot = {codigo: (nome or "Não associado") for codigo, nome in query_snapshot}
            set_snapshot = set(mapa_snapshot.keys())

            codigos_encontrados = set_catalogo.intersection(set_snapshot)
            codigos_faltando = set_catalogo.difference(set_snapshot)
            codigos_novos = set_snapshot.difference(set_catalogo)

            itens_encontrados = [{"codigo_rfid": c, "nome_equip": mapa_catalogo[c]} for c in sorted(list(codigos_encontrados))]
            itens_faltando = [{"codigo_rfid": c, "nome_equip": mapa_catalogo[c]} for c in sorted(list(codigos_faltando))]
            itens_novos = [{"codigo_rfid": c, "nome_equip": mapa_snapshot[c]} for c in sorted(list(codigos_novos))]

        # Cenário 2: Inventários seguintes -> Compara com o Snapshot Anterior
        elif snapshot_count > 1 and snapshot_atual and snapshot_anterior:
            comparison_mode = "snapshot"
            # (Esta é a lógica original de comparação entre dois snapshots)
            def get_data_from_snapshot(snapshot_id):
                items = db.query(Product.codigo_rfid, Equipamento.nome_equip)\
                          .join(InventoryItem, InventoryItem.product_id == Product.id)\
                          .outerjoin(Product.equipamento)\
                          .filter(InventoryItem.snapshot_id == snapshot_id).all()
                return {codigo: (nome or "Não associado") for codigo, nome in items}

            map_atual = get_data_from_snapshot(snapshot_atual.id)
            map_anterior = get_data_from_snapshot(snapshot_anterior.id)
            
            set_atual = set(map_atual.keys())
            set_anterior = set(map_anterior.keys())
            todos_codigos = sorted(list(set_atual | set_anterior))
            
            for codigo in todos_codigos:
                status = "Mantido"
                if codigo in set_atual and codigo not in set_anterior: status = "Adicionado"
                elif codigo not in set_atual and codigo in set_anterior: status = "Deletado"
                tabela_unificada.append({ "codigo_rfid": codigo, "item_atual": map_atual.get(codigo, "---"), "item_anterior": map_anterior.get(codigo, "---"), "status": status })

    rfid_serial_port = settings.get('serial_port', 'Não configurada')

    return templates.TemplateResponse("inventario_list.html", {
        "request": request, "all_datacenters": all_datacenters, "current_dc_id": datacenter_id,
        "inventario_atual": inventario_atual_formatado, "tabela_unificada": tabela_unificada,
        "snapshot_atual": snapshot_atual, "snapshot_anterior": snapshot_anterior,
        "current_view": view, "rfid_serial_port": rfid_serial_port,
        "itens_encontrados": itens_encontrados, "itens_faltando": itens_faltando,
        "itens_novos": itens_novos, "comparison_mode": comparison_mode
    })

@app.post("/inventario/salvar", name="save_inventory_snapshot")
def inventory_save(
    request: Request, db: Session = Depends(get_db),
    codigos: List[str] = Form(...), tipos: List[int] = Form(...),
    datacenter_id: int = Form(...)
):
    # #- LÓGICA CORRIGIDA para a estrutura de Catálogo (Product)
    if not datacenter_id:
        raise HTTPException(status_code=400, detail="Datacenter não especificado.")

    # 1. Obter ou criar os produtos no "catálogo" mestre
    produtos_processados = []
    for codigo_rfid, tipo_id in zip(codigos, tipos):
        codigo_rfid = codigo_rfid.strip().upper()
        if not codigo_rfid: continue
        
        produto = db.query(Product).filter(Product.codigo_rfid == codigo_rfid).first()
        if not produto:
            produto = Product(codigo_rfid=codigo_rfid, product_type_id=tipo_id)
            db.add(produto)
        elif produto.product_type_id != tipo_id: # Atualiza o tipo se mudou
            produto.product_type_id = tipo_id
        produtos_processados.append(produto)
    
    db.flush() # Garante que os IDs dos novos produtos sejam gerados

    # 2. Criar o snapshot
    novo_snapshot = InventorySnapshot(datacenter_id=datacenter_id)
    db.add(novo_snapshot)
    db.flush()

    # 3. Criar os itens de inventário (a ligação)
    itens_vistos = set()
    itens_para_salvar = []
    for p in produtos_processados:
        if p.id in itens_vistos: continue
        itens_para_salvar.append(InventoryItem(snapshot_id=novo_snapshot.id, product_id=p.id))
        itens_vistos.add(p.id)
    
    if itens_para_salvar:
        db.add_all(itens_para_salvar)
        db.commit()
    else:
        db.rollback()

    redirect_url = request.url_for("list_inventario").include_query_params(datacenter_id=datacenter_id)
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@app.get("/inventario/download", name="download_inventory_csv")
def download_inventory_csv(
    db: Session = Depends(get_db),
    datacenter_id: Optional[int] = Query(None)
):
    """
    Gera e faz o download de um arquivo CSV para o inventário mais recente,
    filtrado opcionalmente por datacenter.
    """
    snapshot_query = db.query(InventorySnapshot)
    if datacenter_id:
        snapshot_query = snapshot_query.filter(InventorySnapshot.datacenter_id == datacenter_id)
    
    ultimo_snapshot = snapshot_query.order_by(InventorySnapshot.created_on.desc()).first()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        # --- MUDANÇA: Cabeçalho do CSV atualizado ---
        writer.writerow(["codigo_rfid", "equipamento_associado"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        if ultimo_snapshot:
            # --- MUDANÇA: Query atualizada para buscar Equipamento em vez de ProductType ---
            itens = db.query(Product.codigo_rfid, Equipamento.nome_equip)\
                      .join(InventoryItem, InventoryItem.product_id == Product.id)\
                      .outerjoin(Equipamento, Product.equipamento_id == Equipamento.id)\
                      .filter(InventoryItem.snapshot_id == ultimo_snapshot.id)\
                      .order_by(Product.codigo_rfid)\
                      .all()
            
            # --- MUDANÇA: Loop atualizado para usar os novos dados ---
            for codigo, nome_equip in itens:
                # Usa 'nome_equip' e adiciona "N/A" se for nulo
                writer.writerow([codigo, nome_equip or "N/A"])
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    filename = f"inventario_dc_{datacenter_id}.csv" if datacenter_id else "inventario.csv"
    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===================================================================
# SEÇÃO 6: STARTUP, SHUTDOWN E TAREFAS EM BACKGROUND
# ===================================================================
def purge_old_events():
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
        deleted_count = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
        db.commit()
        if deleted_count > 0:
            logger.info(f"[main] Limpeza de eventos antigos: {deleted_count} registros removidos.")
    except Exception as e:
        logger.error(f"ERRO durante a limpeza de eventos: {e}")
        db.rollback()
    finally:
        db.close()

def start_cleanup_scheduler():
    def loop():
        while True:
            time.sleep(CLEANUP_INTERVAL_SEC)
            purge_old_events()
    threading.Thread(target=loop, daemon=True).start()

running_tasks = {} # Dicionário global para guardar as nossas tarefas

async def check_background_tasks_health():
    """Tarefa de background que monitoriza as outras tarefas."""
    while True:
        await asyncio.sleep(60) # A cada minuto
        for name, task in running_tasks.items():
            if task.done() and not task.cancelled():
                # A tarefa terminou, mas não foi cancelada! Provavelmente falhou.
                try:
                    # Chamar task.result() vai levantar a exceção que causou a falha
                    task.result()
                except Exception as e:
                    logger.critical(
                        f"[HEALTH CHECK] A TAREFA CRÍTICA '{name}' FALHOU: {e}",
                        exc_info=True
                    )
                    # Ação a tomar: Poderíamos tentar reiniciar a tarefa ou o servidor.


# ===================================================================
# SEÇÃO 7: NOVAS APIS DE GERENCIAMENTO DO CATÁLOGO
# ===================================================================

@app.get("/cadastro", name="cadastro_page")
def cadastro_page(request: Request):
    """Renderiza a nova página de Cadastro e Gestão."""
    return templates.TemplateResponse("cadastro.html", {"request": request})


# --- API para Fabricantes ---
@app.get("/api/fabricantes", response_model=List[FabricanteSchema])
def list_fabricantes(db: Session = Depends(get_db)):
    return db.query(Fabricante).order_by(Fabricante.nome).all()

@app.post("/api/fabricantes", response_model=FabricanteSchema, status_code=status.HTTP_201_CREATED)
def create_fabricante(fabricante_in: FabricanteCreate, db: Session = Depends(get_db)):
    if db.query(Fabricante).filter(Fabricante.nome == fabricante_in.nome).first():
        raise HTTPException(status_code=409, detail="Um fabricante com este nome já existe.")
    novo_fabricante = Fabricante(**fabricante_in.model_dump())
    db.add(novo_fabricante)
    db.commit()
    db.refresh(novo_fabricante)
    return novo_fabricante

@app.put("/api/fabricantes/{fab_id}", response_model=FabricanteSchema)
def update_fabricante(fab_id: int, fabricante_in: FabricanteCreate, db: Session = Depends(get_db)):
    fab_db = db.query(Fabricante).get(fab_id)
    if not fab_db:
        raise HTTPException(status_code=404, detail="Fabricante não encontrado.")
    fab_db.nome = fabricante_in.nome
    db.commit()
    db.refresh(fab_db)
    return fab_db

@app.delete("/api/fabricantes/{fab_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fabricante(fab_id: int, db: Session = Depends(get_db)):
    fab_db = db.query(Fabricante).get(fab_id)
    if not fab_db:
        raise HTTPException(status_code=404, detail="Fabricante não encontrado.")
    if fab_db.equipamentos:
        raise HTTPException(status_code=400, detail="Não é possível excluir. Este fabricante está em uso por equipamentos.")
    db.delete(fab_db)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

# --- API para Localizações (DataCenter) ---
@app.get("/api/localizacoes", response_model=List[DataCenterSchema])
def list_localizacoes(db: Session = Depends(get_db)):
    return db.query(DataCenter).order_by(DataCenter.nome).all()

@app.post("/api/localizacoes", response_model=DataCenterSchema, status_code=status.HTTP_201_CREATED)
def create_localizacao(dc_in: DataCenterCreate, db: Session = Depends(get_db)):
    if db.query(DataCenter).filter(DataCenter.nome == dc_in.nome).first():
        raise HTTPException(status_code=409, detail="Uma localização com este nome já existe.")
    nova_loc = DataCenter(**dc_in.model_dump())
    db.add(nova_loc)
    db.commit()
    db.refresh(nova_loc)
    return nova_loc
    
# Adicione PUT e DELETE para localizações se precisar...

# --- API para Bastidores (Racks) ---
@app.get("/api/bastidores", response_model=List[BastidorSchema])
def list_bastidores(
    db: Session = Depends(get_db),
    localizacao_id: Optional[int] = Query(None) # NOVO: Parâmetro de filtro
):
    query = db.query(Bastidor).order_by(Bastidor.codigo_bast)
    if localizacao_id:
        query = query.filter(Bastidor.localizacao_id == localizacao_id)
    return query.all()

@app.post("/api/bastidores", response_model=BastidorSchema, status_code=status.HTTP_201_CREATED)
def create_bastidor(bastidor_in: BastidorCreate, db: Session = Depends(get_db)):
    if not db.query(DataCenter).get(bastidor_in.localizacao_id):
        raise HTTPException(status_code=404, detail="A localização especificada não existe.")
    if db.query(Bastidor).filter(Bastidor.codigo_bast == bastidor_in.codigo_bast).first():
        raise HTTPException(status_code=409, detail="Um bastidor com este código já existe.")
    novo_bastidor = Bastidor(**bastidor_in.model_dump())
    db.add(novo_bastidor)
    db.commit()
    db.refresh(novo_bastidor)
    return novo_bastidor

@app.delete("/api/bastidores/{b_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bastidor(b_id: int, db: Session = Depends(get_db)):
    bastidor_db = db.query(Bastidor).get(b_id)
    if not bastidor_db:
        raise HTTPException(status_code=404, detail="Bastidor não encontrado.")
    if bastidor_db.equipamentos:
        raise HTTPException(status_code=400, detail="Não é possível excluir. Este bastidor já contém equipamentos.")
    db.delete(bastidor_db)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
    
@app.get("/api/equipamento-tipos", response_model=List[EquipamentoTipoSchema])
def list_equipamento_tipos(db: Session = Depends(get_db)):
    return db.query(EquipamentoTipo).order_by(EquipamentoTipo.nome).all()

@app.post("/api/equipamento-tipos", response_model=EquipamentoTipoSchema, status_code=status.HTTP_201_CREATED)
def create_equipamento_tipo(tipo_in: EquipamentoTipoCreate, db: Session = Depends(get_db)):
    if not db.query(Fabricante).get(tipo_in.fabricante_id):
        raise HTTPException(status_code=404, detail="Fabricante não encontrado.")
    if db.query(EquipamentoTipo).filter(EquipamentoTipo.nome == tipo_in.nome).first():
        raise HTTPException(status_code=409, detail="Já existe um modelo de equipamento com este nome.")
    
    novo_tipo = EquipamentoTipo(**tipo_in.model_dump())
    db.add(novo_tipo)
    db.commit()
    db.refresh(novo_tipo)
    return novo_tipo

# --- ADICIONADO: Rota para Editar (Update) um Modelo de Equipamento ---
@app.put("/api/equipamento-tipos/{tipo_id}", response_model=EquipamentoTipoSchema)
def update_equipamento_tipo(tipo_id: int, tipo_in: EquipamentoTipoCreate, db: Session = Depends(get_db)):
    """ Atualiza um modelo de equipamento existente. """
    tipo_db = db.query(EquipamentoTipo).get(tipo_id)
    if not tipo_db:
        raise HTTPException(status_code=404, detail="Modelo de equipamento não encontrado.")
    if not db.query(Fabricante).get(tipo_in.fabricante_id):
        raise HTTPException(status_code=404, detail="Fabricante não encontrado.")

    # Atualiza todos os campos com os dados recebidos
    for key, value in tipo_in.model_dump().items():
        setattr(tipo_db, key, value)
    
    db.commit()
    db.refresh(tipo_db)
    return tipo_db

# --- ADICIONADO: Rota para Excluir (Delete) um Modelo de Equipamento ---
@app.delete("/api/equipamento-tipos/{tipo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_equipamento_tipo(tipo_id: int, db: Session = Depends(get_db)):
    """ Exclui um modelo de equipamento. """
    tipo_db = db.query(EquipamentoTipo).get(tipo_id)
    if not tipo_db:
        raise HTTPException(status_code=404, detail="Modelo de equipamento não encontrado.")
    
    # Verificação de segurança: não permite excluir se o modelo estiver em uso
    if tipo_db.instancias:
        raise HTTPException(status_code=400, detail="Não é possível excluir. Este modelo já está sendo utilizado por instâncias de equipamentos.")
        
    db.delete(tipo_db)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

# --- ATUALIZADO: API Principal para criar uma "Instância de Equipamento" ---
@app.post("/api/equipamentos/instancia", status_code=status.HTTP_201_CREATED)
def create_equipamento_instancia(instancia_in: EquipamentoInstanciaCreate, db: Session = Depends(get_db)):
    # Validações
    if db.query(Equipamento).filter(Equipamento.nome_equip == instancia_in.nome_equip).first():
        raise HTTPException(status_code=409, detail="Já existe um equipamento com este nome (hostname).")
    if db.query(Product).filter(Product.codigo_rfid == instancia_in.codigo_rfid).first():
        raise HTTPException(status_code=409, detail="Este código RFID já está em uso.")
    if not db.query(EquipamentoTipo).get(instancia_in.equipamento_tipo_id):
        raise HTTPException(status_code=404, detail="O modelo de equipamento selecionado não foi encontrado.")
    if not db.query(Bastidor).get(instancia_in.bastidor_id):
        raise HTTPException(status_code=404, detail="O bastidor selecionado não foi encontrado.")

    try:
        # 1. Cria a Instância do Equipamento
        nova_instancia = Equipamento(
            nome_equip=instancia_in.nome_equip,
            equipamento_tipo_id=instancia_in.equipamento_tipo_id,
            bastidor_id=instancia_in.bastidor_id
        )
        db.add(nova_instancia)

        # 2. Cria a etiqueta RFID e a associa à instância
        novo_produto = Product(codigo_rfid=instancia_in.codigo_rfid, equipamento=nova_instancia)
        db.add(novo_produto)
        
        db.commit()
        db.refresh(nova_instancia)
        
        logger.info(f"Instância de equipamento '{nova_instancia.nome_equip}' criada e associada ao RFID '{novo_produto.codigo_rfid}'.")
        return {"detail": "Equipamento cadastrado com sucesso!"}
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao criar instância de equipamento: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro interno ao salvar o equipamento.")


@app.get("/equipamentos", name="list_equipamentos")
def list_equipamentos(
    request: Request,
    db: Session = Depends(get_db),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("nome_equip"),
    order: Optional[str] = Query("asc"),
    # --- MUDANÇA 1: Alterado o tipo de 'int' para 'str' para aceitar valores vazios ---
    filter_localizacao_id: Optional[str] = Query(None),
    filter_modelo_id: Optional[str] = Query(None),
    filter_bastidor_id: Optional[str] = Query(None)
):
    query = db.query(Equipamento).options(
        joinedload(Equipamento.product),
        joinedload(Equipamento.equipamento_tipo).joinedload(EquipamentoTipo.fabricante),
        joinedload(Equipamento.bastidor).joinedload(Bastidor.localizacao)
    )

    localizacao_id_int = int(filter_localizacao_id) if filter_localizacao_id and filter_localizacao_id.isdigit() else None
    modelo_id_int = int(filter_modelo_id) if filter_modelo_id and filter_modelo_id.isdigit() else None
    bastidor_id_int = int(filter_bastidor_id) if filter_bastidor_id and filter_bastidor_id.isdigit() else None

    if localizacao_id_int:
        query = query.join(Equipamento.bastidor).filter(Bastidor.localizacao_id == localizacao_id_int)
    if modelo_id_int:
        query = query.filter(Equipamento.equipamento_tipo_id == modelo_id_int)
    if bastidor_id_int:
        query = query.filter(Equipamento.bastidor_id == bastidor_id_int)


    # Lógica de busca atualizada
    if search:
        search_term = f"%{search}%"
        query = query.join(Equipamento.product).join(Equipamento.equipamento_tipo).join(EquipamentoTipo.fabricante).join(Equipamento.bastidor).join(Bastidor.localizacao).filter(
            or_(
                Equipamento.nome_equip.ilike(search_term),
                EquipamentoTipo.nome.ilike(search_term),
                EquipamentoTipo.modelo.ilike(search_term),
                Product.codigo_rfid.ilike(search_term),
                Fabricante.nome.ilike(search_term),
                Bastidor.codigo_bast.ilike(search_term),
                DataCenter.nome.ilike(search_term)
            )
        )

    # Lógica de ordenação atualizada
    sortable_columns = {
        "nome_equip": Equipamento.nome_equip,
        "rfid": Product.codigo_rfid,
        "modelo_nome": EquipamentoTipo.nome,
        "fabricante": Fabricante.nome,
        "modelo_pn": EquipamentoTipo.modelo,
        "bastidor": Bastidor.codigo_bast,
        "localizacao": DataCenter.nome,
    }

    # Garante que os joins necessários para a ordenação existam
    if sort_by in ["rfid"]: query = query.join(Product)
    if sort_by in ["modelo_nome", "fabricante", "modelo_pn"]: query = query.join(Equipamento.equipamento_tipo).join(EquipamentoTipo.fabricante)
    if sort_by in ["bastidor", "localizacao"]: query = query.join(Equipamento.bastidor).join(Bastidor.localizacao)

    sort_column = sortable_columns.get(sort_by, Equipamento.nome_equip)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    equipamentos = query.all()

    all_localizacoes = db.query(DataCenter).order_by(DataCenter.nome).all()
    all_modelos = db.query(EquipamentoTipo).order_by(EquipamentoTipo.nome).all()

    # --- MUDANÇA IMPORTANTE AQUI ---
    # A lista de bastidores agora é filtrada pela localização, se houver uma selecionada
    bastidores_query = db.query(Bastidor).order_by(Bastidor.codigo_bast)
    if localizacao_id_int:
        bastidores_query = bastidores_query.filter(Bastidor.localizacao_id == localizacao_id_int)
    all_bastidores = bastidores_query.all()
    
    current_filters = {
        "localizacao_id": localizacao_id_int,
        "modelo_id": modelo_id_int,
        "bastidor_id": bastidor_id_int
    }

    return templates.TemplateResponse("equipamentos_list.html", {
        "request": request,
        "equipamentos": equipamentos,
        "current_filters": current_filters,
        "all_localizacoes": all_localizacoes,
        "all_modelos": all_modelos,
        "all_bastidores": all_bastidores,
    })

@app.on_event("startup")
async def on_startup():
    logger.info("[main] Startup: Iniciando serviços em background.")
    running_tasks["aggregator"] = asyncio.create_task(main_aggregator_loop())
    running_tasks["liveness_check"] = asyncio.create_task(check_esp_liveness())
    running_tasks["health_check"] = asyncio.create_task(check_background_tasks_health())
    running_tasks["esp_status_updater"] = asyncio.create_task(batch_update_esp_status())
    mqtt_client.start_mqtt_client()
    start_cleanup_scheduler()

    await asyncio.sleep(5) 
    
    logger.info("[main] Startup: Enviando comando de reconfiguração para todas as ESPs.")    
    command_payload = {"command": "fetch_config"} 
    
    mqtt_client.client.publish(
        topic=settings.get("mqtt_esp_command_topic"), 
        payload=json.dumps(command_payload),
        qos=1 
    )
    logger.info("[main] Startup: Comando de sincronização enviado.")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app", host=settings.get("ip", "0.0.0.0"),
        port=int(settings.get("port", 8000)), reload=True
    )