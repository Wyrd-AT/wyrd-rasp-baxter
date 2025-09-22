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
from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_, desc, asc

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest
from starlette.exceptions import WebSocketException 
from starlette.responses import PlainTextResponse
from collections import defaultdict

from .models import (
    engine, SessionLocal, Asset, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, Andar, init_db
)
from .presence import check_presence
from .aggregator import main_aggregator_loop, batch_update_asset_assignments, _asset_realtime_state
from .services import force_asset_removal, release_assets_for_offline_esp
from . import mqtt_client
from .aggregator import main_aggregator_loop, batch_update_asset_assignments, _asset_realtime_state
from .config import settings
from .auth import authenticate_admin
from .dispatcher import dispatch_event
from . import handshake_client

logger.info("[main] Módulo carregado para a versão MULTI-ATIVO.")

HISTORY_RETENTION_DAYS = int(settings.get('history_retention_days', 7))
EVENT_PAGE_SIZE = int(settings.get('event_page_size', 25))
CLEANUP_INTERVAL_SEC = int(settings.get('cleanup_interval_sec', 3600))
ESP_TIMEOUT_SEC = int(settings.get('esp_timeout_sec', 150)) 
WIFI_FAILURE_TOLERANCE = int(settings.get('wifi_failure_tolerance', 3)) 
ESP_STATUS_UPDATE_INTERVAL_SEC = int(settings.get('esp_status_interval_sec', 90))
WIFI_GUARDIAN_INTERVAL_SEC = int(settings.get('monitor_wifi_interval_sec', 60))

pending_rssi_requests = {}
_wifi_failure_counts = defaultdict(int)
_wifi_presence_cache = {}

_handshake_challenge_sent = {}

try:
    base_path = sys._MEIPASS
except Exception:
    base_path = os.path.dirname(os.path.abspath(__file__))

templates_path = os.path.join(base_path, "web/templates")
static_path = os.path.join(base_path, "web/static")

init_db()


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

# Adiciona as views ao painel de admin
admin.add_view(AssetAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(QuartoAdmin)
admin.add_view(ReceivedEventAdmin)

# --- Dependência do Banco de Dados ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

STATE_LOG_INTERVAL_SEC = int(settings.get('state_log_interval_sec', 30))

# Crie um logger específico para os estados
state_logger = logging.getLogger('aggregator_state')

async def log_aggregator_state_task():
    """
    Tarefa de background que periodicamente registra o estado em memória de cada
    ativo do aggregator em um formato JSON estruturado.
    """
    logger.info(f"[STATE_LOGGER] Serviço de log de estado do agregador iniciado. Intervalo: {STATE_LOG_INTERVAL_SEC}s.")
    while True:
        await asyncio.sleep(STATE_LOG_INTERVAL_SEC)
        
        if not _asset_realtime_state:
            continue

        now = time.time()
        # Itera sobre uma cópia para evitar problemas de concorrência durante a iteração
        for mac, state in list(_asset_realtime_state.items()):
            # Monta um dicionário com os dados mais relevantes do estado do ativo
            state_snapshot = {
                "timestamp": now,
                "mac_beacon": state.mac,
                "disappearance_count": state.disappearance_count,
                "candidate_quarto_id": state.candidate_quarto_id,
                "candidate_since": state.candidate_since,
                "pending_quarto_id": state.pending_quarto_id,
                "pending_since": state.pending_wifi_check_since,
                "disappeared_since": state.disappeared_since,
                "wifi_unseen_since": state.wifi_unseen_since,
                "last_strongest_signal": state.last_strongest_signal,
                "readings_count": len(state.readings),
                "readings": state.readings # Loga todas as leituras atuais
            }
            # Usa o logger para registrar o estado como uma linha JSON
            state_logger.info(json.dumps(state_snapshot))

            
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    receiver_task = None
    pinger_task = None
    
    try:
        logger.info("[WebSocket] Nova conexão estabelecida. Cliente: %s, ID: %s", websocket.client, client_id)
        await websocket.send_text(json.dumps({"type": "CONNECTION_INFO", "client_id": client_id}))
        logger.info("[WebSocket] ID '%s' enviado para o cliente.", client_id)

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
    Por enquanto, ela apenas redireciona para a planta, como pedido.
    """
    return RedirectResponse(url=request.url_for("view_planta"), status_code=303)

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

@app.post("/api/presence/confirm", name="confirm_presence_handshake", status_code=200)
async def confirm_presence_handshake(request: Request, db: Session = Depends(get_db)):
    """
    Endpoint de callback que recebe a confirmação do sistema final e
    AVISA o agregador para atualizar seu estado interno.
    """
    data = await request.json()
    nome_cama = data.get("cama")
    status_confirmado = data.get("status")

    if not nome_cama or status_confirmado != "TRUE":
        raise HTTPException(status_code=400, detail="Payload inválido.")

    asset = db.query(Asset).filter(Asset.nome_ativo == nome_cama).first()
    if not asset:
        logger.warning(f"[HANDSHAKE-CALLBACK] Confirmação recebida para a cama '{nome_cama}', mas ela não foi encontrada no DB.")
        return PlainTextResponse("Asset not found")

    # --- LÓGICA ATUALIZADA ---
    # Em vez de modificar uma variável local, chama a função no agregador.
    aggregator.confirm_asset_by_handshake(asset.mac_beacon)
    
    return PlainTextResponse("OK")

@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("login_page"), status_code=303)

@app.post("/embarcados/test_rssi", name="test_rssi_esp")
async def test_rssi_esp(request: Request, db: Session = Depends(get_db)):
    """
    (VERSÃO HSA) Gera um relatório de RSSI lendo o estado atual da memória
    do agregador e envia para o cliente via WebSocket.
    """
    data = await request.json()
    embarcado_id = data.get("embarcado_id")
    client_id = data.get("client_id")

    if not embarcado_id or not client_id:
        raise HTTPException(status_code=400, detail="embarcado_id e client_id são necessários.")

    embarcado = db.query(Embarcado).get(embarcado_id)
    if not embarcado:
        raise HTTPException(status_code=404, detail="Embarcado não encontrado.")

    logger.info(f"Gerando relatório RSSI para a ESP '{embarcado.id_esp}' a pedido do cliente '{client_id}'.")

    report_data = []
    # Itera sobre o estado em tempo real dos ativos na memória do aggregator
    for mac, state in _asset_realtime_state.items():
        if embarcado.id_esp in state.readings:
            reading = state.readings[embarcado.id_esp]
            report_data.append({
                "mac": mac,
                "rssi": reading.get("rssi", -1000)
            })

    # Monta a mensagem para enviar via WebSocket
    websocket_message = {
        "type": "RSSI_REPORT",
        "esp_id": embarcado.id_esp,
        "report": report_data
    }

    # Envia o relatório de volta para o cliente específico que solicitou
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
async def reboot_esp(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    """
    (VERSÃO COMBINADA) Reseta o estado do quarto no servidor (remove a cama)
    E envia o comando de reinicialização para o ESP.
    """
    embarcado = db.query(Embarcado).get(embarcado_id)
    if embarcado:
        # 1. Lógica do "Resetar Estado" (executada primeiro)
        if embarcado.quarto_id:
            logger.info(f"Resetando estado do quarto para o embarcado '{embarcado.id_esp}' antes de reiniciar.")
            asset_no_quarto = db.query(Asset).filter(Asset.quarto_id == embarcado.quarto_id).first()
            if asset_no_quarto:
                await force_asset_removal(
                    db=db, 
                    asset_id=asset_no_quarto.id,
                    details=f"Remoção forçada pelo operador via reinicialização do embarcado '{embarcado.id_esp}'."
                )

        # 2. Lógica do "Reiniciar" (executada em seguida)
        logger.info("Enviando comando 'REBOOT' para a ESP '%s'.", embarcado.id_esp)
        command = {"type": "command", "data": {"name": "REBOOT"}}
        mqtt_client.publish_command_to_esp(esp_id=embarcado.id_esp, command=command)

    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.post("/settings/update", name="update_settings")
def update_settings(
    request: Request, db: Session = Depends(get_db), 
    rssi_threshold: str = Form(...),
    inercia_entrada: str = Form(...), # Novo
    inercia_saida: str = Form(...)   # Novo
):
    settings_data = {
        "rssi_threshold": rssi_threshold,
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

GUARDIAN_LOG_INTERVAL_SEC = 120 # Logar a cada 120 segundos (2 minutos)

async def wifi_guardian_task_unificada(db: Session):
    """
    Esta tarefa agora usa o cliente TCP para verificar a presença de ativos
    pendentes e confirmados.
    """
    try:
        # Pega a lista de ativos PENDENTES
        pending_assets = aggregator.get_pending_states_for_ui()
        assets_to_check = {asset['ativo_mac']: asset for asset in pending_assets}

        # Pega a lista de ativos CONFIRMADOS
        confirmed_assets_db = db.query(Asset).filter(Asset.quarto_id != None).all()
        for asset in confirmed_assets_db:
            if asset.mac_beacon not in assets_to_check:
                assets_to_check[asset.mac_beacon] = {"nome_ativo": asset.nome_ativo, "modelo": asset.modelo}
        
        if not assets_to_check:
            return

        for mac, asset_info in assets_to_check.items():
            logger.debug(f"[GUARDIAN-TCP] Verificando presença para {mac}...")
            
            challenge_payload = {
                "cama": asset_info.get("nome_ativo"),
                "modelo": asset_info.get("modelo"),
                "dataOn": datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            }
            
            # Chama o cliente e aguarda a resposta True/False
            is_present = await handshake_client.send_challenge_and_get_response(challenge_payload)

            if is_present:
                # Se presente, avisa o agregador para atualizar seu estado.
                aggregator.confirm_asset_by_handshake(mac)

    except Exception as e:
        logger.error(f"[GUARDIAN-TCP] Erro crítico na tarefa: {e}", exc_info=True)

async def main_guardian_loop():
    """
    Loop principal que executa a tarefa do guardião de Wi-Fi continuamente.
    """
    logger.info("[GUARDIAN-UNIFICADO] Serviço iniciado.")
    while True:
        db = SessionLocal()
        try:
            await wifi_guardian_task_unificada(db)
        finally:
            db.close()
        await asyncio.sleep(WIFI_GUARDIAN_INTERVAL_SEC) # Espera 30 segundos

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
            
            esps_a_verificar = db.query(Embarcado).filter(
                Embarcado.last_seen != None,
                Embarcado.status_rede == 'online'
            ).all()

            esps_que_ficaram_offline = []
            for emb in esps_a_verificar:
                last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
                if last_seen_utc < cutoff_time:
                    esps_que_ficaram_offline.append(emb)
            
            if esps_que_ficaram_offline:
                logger.warning("[LIVENESS] ESPs considerados offline nesta verificação: %s", [e.id_esp for e in esps_que_ficaram_offline])
                
                for emb in esps_que_ficaram_offline:
                    await release_assets_for_offline_esp(db, emb.id_esp)
                    
                    emb.status_rede = 'offline'
                    
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
    logger.info("[BATCH-UPDATE-ESP] Serviço de atualização de status de embarcados iniciado.")
    while True:
        await asyncio.sleep(ESP_STATUS_UPDATE_INTERVAL_SEC)
        
        status_updates = mqtt_client.get_and_clear_status_cache()
        if not status_updates:
            # <-- LOG 1: INFORMA QUANDO A TAREFA RODA, MAS NÃO HÁ NADA A FAZER
            logger.info("[BATCH-UPDATE-ESP] Verificação executada. Nenhum status novo no cache.")
            continue

        # <-- LOG 2: INFORMA QUE HÁ TRABALHO A SER FEITO (JÁ EXISTIA, MAS É IMPORTANTE)
        logger.info(f"[BATCH-UPDATE-ESP] Atualizando status de {len(status_updates)} embarcados no banco de dados.")
        db = SessionLocal()
        try:
            esp_ids_to_update = list(status_updates.keys())
            embarcados_to_update = db.query(Embarcado).filter(Embarcado.id_esp.in_(esp_ids_to_update)).all()
            
            updated_count = 0
            for emb in embarcados_to_update:
                if emb.id_esp in status_updates:
                    data = status_updates[emb.id_esp]
                    emb.last_seen = data["last_seen"]
                    if "wifi_signal" in data:
                        emb.wifi_signal = data["wifi_signal"]
                    if emb.status_rede == 'offline':
                        emb.status_rede = 'online'
                    updated_count += 1
            
            db.commit()
            # <-- LOG 3: CONFIRMA QUE A OPERAÇÃO FOI BEM-SUCEDIDA
            #logger.info(f"[BATCH-UPDATE-ESP] {updated_count} registros de embarcados foram atualizados com sucesso.")

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
    """
    Endpoint único para a ESP se anunciar e obter a sua configuração de operação.
    """
    logger.info(f"HANDSHAKE recebido da ESP: {id_esp} (MAC: {mac}, IP: {ip}, FW: {fw})")

    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    if embarcado:
        embarcado.mac_address = mac
        embarcado.ip_address = ip
        db.commit()
    
    else:
        logger.warning(f"Handshake recebido de um embarcado não cadastrado: {id_esp}")

    all_assets = db.query(Asset.mac_beacon).filter(Asset.mac_beacon.isnot(None)).all()
    whitelist = [m for m, in all_assets]

    logger.info(f"Enviando configuração para {id_esp}: {len(whitelist)} ativos na whitelist.")

    return {
        "whitelist": whitelist
    }

def _notify_esps_of_asset_change():
    """
    Publica o comando 'fetch_config' no tópico MQTT geral para que todas
    as ESPs atualizem sua whitelist de ativos.
    """
    logger.info("[main] Notificando todas as ESPs sobre alteração na lista de ativos.")
    
    # Payload do comando que as ESPs esperam
    command_payload = {"command": "fetch_config"}
    
    # Publica no tópico geral que todas as ESPs escutam
    mqtt_client.client.publish(
        topic=settings.get("mqtt_esp_command_topic"),
        payload=json.dumps(command_payload),
        qos=1
    )
    logger.info("[main] Comando de sincronização enviado para o tópico geral.")

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
    query = db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar))
    if search:
        search_term = f"%{search}%"
        query = query.join(Embarcado.quarto).join(Quarto.andar).filter(
            or_(Embarcado.id_esp.ilike(search_term), Quarto.nome.ilike(search_term), Andar.nome.ilike(search_term), Embarcado.mac_address.ilike(search_term), Embarcado.ip_address.ilike(search_term))
        )
    
    # --- LÓGICA DE ORDENAÇÃO ---
    sortable_columns = {
        "id_esp": Embarcado.id_esp, "andar": Andar.nome, "quarto": Quarto.nome,
        "status": Embarcado.status_rede, "wifi_signal": Embarcado.wifi_signal, "rssi_min": Embarcado.rssi_threshold,
        "mac_address": Embarcado.mac_address, "ip_address": Embarcado.ip_address
    }
    # Adiciona joins necessários para a ordenação
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
    andar_nome: str = Form(...), # Novo campo
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
    
    # --- LÓGICA DE FILTRO DE QUARTOS DISPONÍVEIS (PARA EDIÇÃO) ---
    # 1. Pega os IDs dos quartos atribuídos a OUTROS embarcados.
    assigned_quarto_ids = {
        emb.quarto_id for emb in db.query(Embarcado).filter(
            Embarcado.id != embarcado_id, # Exclui o embarcado atual da verificação
            Embarcado.quarto_id.isnot(None)
        ).all()
    }

    aggregator.flag_for_reload() # <-- ADICIONAR ESTA LINHA
    
    # 2. Busca os quartos que não estão na lista de atribuídos.
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()
    
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": db.query(Embarcado).options(joinedload(Embarcado.quarto)).order_by(Embarcado.id_esp).all(),
        "available_quartos": available_quartos, # <-- Passa a lista filtrada
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": emb_para_editar,
        "search": None, "global_settings": get_global_settings(db),
        "current_filters": {"search": None, "sort_by": "id_esp", "order": "asc"}
    })

# Em main.py

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(
    request: Request, 
    embarcado_id: int, 
    andar_nome: str = Form(...), # Novo campo
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
    if search:
        search_term = f"%{search}%"
        query = query.outerjoin(Asset.quarto).filter(
            or_(Asset.nome_ativo.ilike(search_term), Asset.mac_beacon.ilike(search_term), Asset.mac_address.ilike(search_term), Quarto.nome.ilike(search_term), Asset.tipo_ativo.ilike(search_term), Asset.modelo.ilike(search_term), Asset.fabricante.ilike(search_term))
        )

    # --- LÓGICA DE ORDENAÇÃO ---
    sortable_columns = {
        "nome_ativo": Asset.nome_ativo, "tipo_ativo": Asset.tipo_ativo, "modelo": Asset.modelo,
        "fabricante": Asset.fabricante, "mac_beacon": Asset.mac_beacon, "quarto": Quarto.nome
    }
    # Adiciona join se necessário (outerjoin para não excluir ativos sem quarto)
    if sort_by == "quarto":
        query = query.outerjoin(Asset.quarto)
        
    sort_column = sortable_columns.get(sort_by, Asset.nome_ativo)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    assets = query.all()
    
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": assets,
        "form_action": request.url_for("create_asset"), "asset": None, 
        "current_filters": {"search": search, "sort_by": sort_by, "order": order}
    })


@app.post("/assets", name="create_asset")
def create_asset(
    request: Request, 
    nome_ativo: str = Form(...), 
    mac_address: str = Form(...),  
    mac_beacon: str = Form(...),
    # --- NOVOS CAMPOS DO FORMULÁRIO ---
    tipo_ativo: str = Form(None),
    modelo: str = Form(None),
    fabricante: str = Form(None),
    db: Session = Depends(get_db)
):
    asset = Asset(
        nome_ativo=nome_ativo, 
        mac_address=mac_address.lower(),  
        mac_beacon=mac_beacon.lower(),
        # --- NOVOS DADOS PARA SALVAR ---
        tipo_ativo=tipo_ativo,
        modelo=modelo,
        fabricante=fabricante
    )
    try:
        db.add(asset)
        db.commit()
        aggregator.flag_for_reload()
        _notify_esps_of_asset_change()
    except IntegrityError:
        db.rollback()
        logger.error(f"[main-db] ERRO: Tentativa de criar ativo com nome ou MAC duplicado: {nome_ativo} / {mac_beacon.lower()}")
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar ativo: {e}")
    return RedirectResponse(request.url_for("list_assets"), status_code=303)


@app.get("/assets/{asset_id}/edit", name="edit_asset")
def edit_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    # Esta rota não precisa de mudanças, ela apenas exibe o formulário.
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": db.query(Asset).order_by(Asset.nome_ativo).all(),
        "form_action": request.url_for("update_asset", asset_id=asset_id),
        "asset": db.query(Asset).get(asset_id), "search": None,
        "current_filters": {"search": None, "sort_by": "nome_ativo", "order": "asc"}
    })

@app.post("/assets/{asset_id}/edit", name="update_asset")
def update_asset(
    request: Request, 
    asset_id: int, 
    nome_ativo: str = Form(...), 
    mac_address: str = Form(...),
    mac_beacon: str = Form(...),
    # --- NOVOS CAMPOS DO FORMULÁRIO ---
    tipo_ativo: str = Form(None),
    modelo: str = Form(None),
    fabricante: str = Form(None),
    db: Session = Depends(get_db)
):
    asset = db.query(Asset).get(asset_id)
    if asset:
        asset.nome_ativo = nome_ativo
        asset.mac_address=mac_address.lower()
        asset.mac_beacon = mac_beacon.lower()
        # --- ATUALIZANDO OS NOVOS DADOS ---
        asset.tipo_ativo = tipo_ativo
        asset.modelo = modelo
        asset.fabricante = fabricante
        
        db.commit()
        aggregator.flag_for_reload() 
        _notify_esps_of_asset_change()
            
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/assets/{asset_id}/delete", name="delete_asset")
def delete_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).get(asset_id)
    if asset:
        db.delete(asset)
        db.commit()
        aggregator.flag_for_reload()
        _notify_esps_of_asset_change()
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

# ===================================================================
# SEÇÃO 4.5: ROTAS DA PLANTA BAIXA
# ===================================================================

@app.get("/planta", name="view_planta")
def view_planta(request: Request, db: Session = Depends(get_db)):
    """
    Renderiza a página da planta baixa interativa.
    """
    return templates.TemplateResponse("planta_baixa.html", {"request": request})

@app.get("/api/planta/dados", name="get_planta_dados")
def get_planta_dados(db: Session = Depends(get_db)):
    """
    (VERSÃO MODIFICADA) Endpoint de API que fornece os dados de ocupação,
    INCLUINDO o status de ativos pendentes para a planta baixa.
    """
    # 1. Busca os quartos e os ativos já confirmados (como antes)
    quartos_db = db.query(Quarto).options(
        joinedload(Quarto.assets),
        joinedload(Quarto.embarcados)
    ).order_by(Quarto.id).all()

    # 2. Busca os ativos pendentes da memória do agregador
    pending_states_raw = aggregator.get_pending_states_for_ui()
    pending_map = {
        state['pending_quarto_id']: {
            "nome": state.get("nome_ativo", state.get("ativo_mac")),
            "status": "pendente"
        } for state in pending_states_raw
    }

    lista_quartos_data = []
    sumario = {"quartos_online": 0, "total_ativos": 0}
    now_utc = datetime.now(timezone.utc)
    sao_paulo_tz = timezone(timedelta(hours=-3))

    for quarto in quartos_db:
        # Lógica de status do embarcado (sem alterações)
        status_embarcado = "Offline"
        if quarto.embarcados:
            embarcado = quarto.embarcados[0]
            if embarcado.last_seen:
                last_seen_utc = embarcado.last_seen.replace(tzinfo=timezone.utc)
                if (now_utc - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                    status_embarcado = "Online"
        
        if status_embarcado == "Online":
            sumario["quartos_online"] += 1
        
        # 3. Monta a lista de ativos do quarto, unindo confirmados e pendentes
        ativos_detalhados = []
        for asset in quarto.assets:
            # Busca pelo último evento para pegar a data/hora
            ultimo_evento = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status == 'OK'
            ).order_by(desc(ReceivedEvent.data_on)).first()

            texto_conexao = "Horário indisponível"
            if ultimo_evento and ultimo_evento.data_on:
                data_utc = ultimo_evento.data_on.replace(tzinfo=timezone.utc)
                data_local = data_utc.astimezone(sao_paulo_tz)
                texto_conexao = data_local.strftime("desde %d/%m às %H:%M")
            
            ativos_detalhados.append({"nome": asset.nome_ativo, "status": "confirmado", "texto_conexao": texto_conexao})

        # Adiciona o ativo pendente, se houver um para este quarto
        if quarto.id in pending_map:
            pending_asset = pending_map[quarto.id]
            ativos_detalhados.append({
                "nome": pending_asset["nome"], 
                "status": "pendente",
                "texto_conexao": "Aguardando Wi-Fi"
            })

        sumario["total_ativos"] += len(ativos_detalhados)

        lista_quartos_data.append({
            "id_quarto": f"quarto-{quarto.id}",
            "nome_quarto": quarto.nome,
            "numero_ativos": len(ativos_detalhados),
            "status_embarcado": status_embarcado,
            "ativos": ativos_detalhados
        })

    return JSONResponse(content={
        "quartos": lista_quartos_data,
        "sumario": sumario
    })

@app.get("/api/aggregator/pending_events", name="get_pending_events")
def get_pending_events_from_memory(db: Session = Depends(get_db)): # <-- Adiciona a dependência do DB
    """
    Endpoint que consulta o estado da memória do agregador, enriquece os dados
    com o nome do quarto vindo do DB, e retorna a lista para a UI.
    """
    # 1. Pega os dados brutos da memória do agregador
    pending_states_raw = aggregator.get_pending_states_for_ui()
    
    # 2. Se não houver nada, retorna uma lista vazia
    if not pending_states_raw:
        return JSONResponse(content=[])
        
    quarto_ids = {state['pending_quarto_id'] for state in pending_states_raw if state['pending_quarto_id']}
    
    quartos_map = {q.id: q.nome for q in db.query(Quarto).filter(Quarto.id.in_(quarto_ids)).all()}
    
    response_data = []
    for state in pending_states_raw:
        quarto_id = state.get('pending_quarto_id')
        state['quarto_nome'] = quartos_map.get(quarto_id, "N/A")
        response_data.append(state)
        
    return JSONResponse(content=response_data)

# ===================================================================
# SEÇÃO 5: HISTÓRICO DE EVENTOS E DOWNLOADS
# ===================================================================
@app.get("/events", name="list_events")
def list_events(
    request: Request, db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    # Parâmetros de filtro
    filter_ativo: Optional[str] = Query(None),
    filter_andar: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None),
    # --- NOVOS PARÂMETROS DE BUSCA E ORDENAÇÃO ---
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("data_on"),
    order: Optional[str] = Query("desc")
):
    asset_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}
    pending_events = db.query(ReceivedEvent).filter(ReceivedEvent.status == 'Pendente').order_by(desc(ReceivedEvent.data_on)).all()
    history_query = db.query(ReceivedEvent).filter(ReceivedEvent.status != 'Pendente')

    # --- LÓGICA DE BUSCA ---
    if search:
        search_term = f"%{search}%"
        history_query = history_query.filter(
            or_(ReceivedEvent.ativo.ilike(search_term), ReceivedEvent.status_detail.ilike(search_term), ReceivedEvent.quarto_nome.ilike(search_term))
        )

    # --- LÓGICA DE FILTROS ---
    if filter_andar: history_query = history_query.filter(ReceivedEvent.andar_nome == filter_andar)
    if filter_quarto: history_query = history_query.filter(ReceivedEvent.quarto_nome == filter_quarto)
    if filter_ativo: history_query = history_query.filter(ReceivedEvent.ativo == filter_ativo)
    if filter_action: history_query = history_query.filter(ReceivedEvent.action == filter_action)
    if filter_status: history_query = history_query.filter(ReceivedEvent.status == filter_status)
    if time_filter:
        now = datetime.now(timezone.utc)
        delta = None
        if time_filter == 'daily': delta = timedelta(days=1)
        elif time_filter == 'weekly': delta = timedelta(weeks=1)
        elif time_filter == 'monthly': delta = timedelta(days=30)
        if delta: history_query = history_query.filter(ReceivedEvent.data_on >= now - delta)
            
    # --- LÓGICA DE ORDENAÇÃO ---
    sortable_columns = {
        "data_on": ReceivedEvent.data_on, "ativo": ReceivedEvent.ativo, "quarto": ReceivedEvent.quarto_nome, "andar": ReceivedEvent.andar_nome,
        "action": ReceivedEvent.action, "status": ReceivedEvent.status, "rssi": ReceivedEvent.rssi, "wifi": ReceivedEvent.wifi
    }
    sort_column = sortable_columns.get(sort_by, ReceivedEvent.data_on)
    history_query = history_query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    # Paginação e enriquecimento dos dados...
    total = history_query.count()
    events = history_query.offset((page - 1) * EVENT_PAGE_SIZE).limit(EVENT_PAGE_SIZE).all()

    has_next = (page * EVENT_PAGE_SIZE) < total

    # --- Função de Enriquecimento para AMBAS as listas ---
    sao_paulo_tz = timezone(timedelta(hours=-3))
    def enrich_event_data(event_list):
        for e in event_list:
            e.nome_ativo = asset_map.get(e.ativo, e.ativo)
            e.quarto = e.quarto_nome if e.quarto_nome else "N/A"
            e.nome_cama = e.nome_ativo
            if e.data_on:
                data_utc = e.data_on.replace(tzinfo=timezone.utc)
                data_local = data_utc.astimezone(sao_paulo_tz)
                e.data_str = data_local.strftime("%d/%m/%Y")
                e.hora_str = data_local.strftime("%H:%M:%S")

    enrich_event_data(pending_events)
    enrich_event_data(events)

    # --- Coleta de dados para os menus de filtro ---
    all_assets = db.query(Asset.nome_ativo, Asset.mac_beacon).distinct().order_by(Asset.nome_ativo).all()
    all_action_options = [("GET", "Conectar"), ("OUT", "Desconectar"), ("WARNING", "Alerta")]
    all_status_options = ["OK", "Resolvido", "Confirmado", "Enfileirado", "Ignorado", "Cancelado", "Vencido", "Erro"]
    all_quartos = sorted([q.nome for q in db.query(Quarto).order_by(Quarto.nome).all()])
    all_andares = sorted([a.nome for a in db.query(Andar).order_by(Andar.nome).all()]) 

    return templates.TemplateResponse("events_list.html", {
        "request": request,
        "pending_events": pending_events,
        "events": events,
        "page": page,
        "has_next": has_next,
        "all_assets": all_assets,
        "all_action_options": all_action_options,
        "all_status_options": all_status_options,
        "all_quartos": all_quartos,
        "all_andares": all_andares,
        "current_filters": {
            "ativo": filter_ativo, "andar": filter_andar, "quarto": filter_quarto, 
            "action": filter_action, "status": filter_status, "time_filter": time_filter, 
            "search": search, "sort_by": sort_by, "order": order
        }
    })


@app.post("/events/{asset_mac}/cancel", name="cancel_pending_event")
def cancel_pending_event(request: Request, asset_mac: str):
    # A função agora chama a nova lógica no agregador que LOGA e DEPOIS limpa.
    success = aggregator.cancel_and_log_manual_pending_event(mac_beacon_to_cancel=asset_mac)
    
    if not success:
        logger.warning(f"Tentativa de cancelar evento pendente para o MAC {asset_mac}, mas não foi encontrado em estado pendente.")

    return RedirectResponse(request.url_for("list_events"), status_code=303)

@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    db: Session = Depends(get_db),
    # Parâmetros de filtro (sem alterações)
    filter_ativo: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None), 
    time_filter: Optional[str] = Query(None)
):
    # Lógica de busca e filtro (sem alterações)
    beacon_to_asset_name_map = {b.mac_beacon: b.nome_ativo for b in db.query(Asset).filter(Asset.mac_beacon.isnot(None)).all()}
    query = db.query(ReceivedEvent)
    if filter_ativo: query = query.filter(ReceivedEvent.ativo == filter_ativo)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
    if filter_quarto: query = query.filter(ReceivedEvent.quarto_nome == filter_quarto)
    if filter_status: query = query.filter(ReceivedEvent.status == filter_status)
    if filter_action: query = query.filter(ReceivedEvent.action == filter_action)

    events = query.order_by(ReceivedEvent.data_on.desc()).all()

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        
        # MUDANÇA 1: Adicionada a coluna "ANDAR" ao cabeçalho
        writer.writerow(["Data/Hora", "Nome do Ativo", "Quarto", "Andar", "Status", "Ação", "RSSI BLE", "RSSI Wi-Fi"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        action_map = {"GET": "Conectar", "OUT": "Desconectar", "ALERTA": "Alerta"}
        
        # MUDANÇA 2: Definido o fuso horário local
        fuso_local = timezone(timedelta(hours=-3))

        for e in events:
            nome_ativo = beacon_to_asset_name_map.get(e.ativo, e.ativo)
            acao_traduzida = action_map.get(e.action, e.action)
            
            data_hora_local_str = ""
            if e.data_on:
                # MUDANÇA 3: Conversão da data/hora de UTC para o fuso local
                data_utc = e.data_on.replace(tzinfo=timezone.utc)
                data_local = data_utc.astimezone(fuso_local)
                data_hora_local_str = data_local.strftime("%d/%m/%Y %H:%M:%S")

            # MUDANÇA 4: Adicionado o dado do andar (e.andar_nome) na linha
            writer.writerow([
                data_hora_local_str,
                nome_ativo, 
                e.quarto_nome or "---", 
                e.andar_nome or "---", # <-- Dado do andar adicionado aqui
                e.status, 
                acao_traduzida, 
                e.rssi, 
                e.wifi
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
    embarcados = db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar)).order_by(Embarcado.id_esp).all()
    
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID DO EMBARCADO", "ANDAR", "QUARTO", "SINAL WI-FI (RSSI)"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for emb in embarcados:
            quarto_nome = emb.quarto.nome if emb.quarto else ""
            andar_nome = emb.quarto.andar.nome if emb.quarto and emb.quarto.andar else ""
            writer.writerow([emb.id_esp, andar_nome, quarto_nome, emb.wifi_signal])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            
    return StreamingResponse(
        iter_csv(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"}
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
                    # Ação a tomar: Poderíamos 64:70:02:5f:e0:40tentar reiniciar a tarefa ou o servidor.

@app.on_event("startup")
async def on_startup():
    logger.info("[main] Startup: Iniciando serviços em background.")
    running_tasks["aggregator"] = asyncio.create_task(main_aggregator_loop())
    running_tasks["liveness_check"] = asyncio.create_task(check_esp_liveness())
    running_tasks["health_check"] = asyncio.create_task(check_background_tasks_health())
    running_tasks["esp_status_updater"] = asyncio.create_task(batch_update_esp_status())
    running_tasks["guardian_unificado"] = asyncio.create_task(main_guardian_loop())
    running_tasks["state_logger"] = asyncio.create_task(log_aggregator_state_task())
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