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
from fastapi import FastAPI, Request, Response, Form, HTTPException, Query, Depends, status, Body
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, or_, desc, asc

from wtforms.fields import SelectField
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest
from starlette.exceptions import WebSocketException 

# --- Importações dos Módulos da Aplicação ---
from .models import (
    engine, SessionLocal, Asset, Embarcado, Quarto,
    ReceivedEvent, GlobalSetting, Andar, PainelVisualizacao,
    TipoDeAtivo, TipoDeQuarto, init_db
)
from .services import batch_update_asset_assignments, release_assets_for_offline_esp, force_asset_removal
from . import mqtt_client
from .aggregator import main_aggregator_loop, _asset_realtime_state
from .config import settings
from .auth import authenticate_admin
from . import bed_mqtt_client
from .bed_mqtt_client import bed_state_queue

logger.info("[main] Módulo carregado para a versão MULTI-ATIVO.")

# --- Constantes e Configuração Inicial ---
HISTORY_RETENTION_DAYS = int(settings.get('history_retention_days', 7))
EVENT_PAGE_SIZE = int(settings.get('event_page_size', 25))
CLEANUP_INTERVAL_SEC = int(settings.get('cleanup_interval_sec', 3600))
ESP_TIMEOUT_SEC = int(settings.get('esp_timeout_sec', 150))
PENDING_MANAGER_INTERVAL_SEC = 30

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

app = FastAPI(title="Wyrd-RTLS")

admin = Admin(app, engine, authentication_backend=authentication_backend)

class TipoDeAtivoAdmin(ModelView, model=TipoDeAtivo):
    name = "Tipo de Ativo"
    name_plural = "Tipos de Ativo"
    icon = "fa-solid fa-shapes"
    column_list = [TipoDeAtivo.nome, TipoDeAtivo.requer_confirmacao_externa, TipoDeAtivo.algoritmo_media,TipoDeAtivo.parametro_media, TipoDeAtivo.precisa_de_despache]
    form_columns = [TipoDeAtivo.nome, TipoDeAtivo.requer_confirmacao_externa, TipoDeAtivo.algoritmo_media, TipoDeAtivo.parametro_media, TipoDeAtivo.precisa_de_despache]

class TipoDeQuartoAdmin(ModelView, model=TipoDeQuarto):
    name = "Tipo de Quarto"
    name_plural = "Tipos de Quarto"
    icon = "fa-solid fa-vector-square"
    column_list = [TipoDeQuarto.nome, TipoDeQuarto.capacidade_maxima, TipoDeQuarto.permite_transicao_direta, TipoDeQuarto.habilita_eventos_integracao]
    form_columns = [TipoDeQuarto.nome, TipoDeQuarto.capacidade_maxima, TipoDeQuarto.permite_transicao_direta,TipoDeQuarto.habilita_eventos_integracao]

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

# --- CLASSES ATUALIZADAS E NOVAS ABAIXO ---

class QuartoAdmin(ModelView, model=Quarto):
    name = "Quarto"
    name_plural = "Quartos"
    icon = "fa-solid fa-door-closed"
    column_list = [Quarto.id, Quarto.nome, Quarto.andar, Quarto.pos_x, Quarto.pos_y, Quarto.quarto_imagem_url]
    # Campos que aparecerão no formulário de edição/criação
    form_columns = [Quarto.andar, Quarto.nome, Quarto.pos_x, Quarto.pos_y, Quarto.quarto_imagem_url]


class AndarAdmin(ModelView, model=Andar):
    name = "Andar"
    name_plural = "Andares"
    icon = "fa-solid fa-layer-group"
    column_list = [Andar.id, Andar.nome, Andar.planta_imagem_url]
    form_columns = [Andar.nome, Andar.planta_imagem_url]


class PainelAdmin(ModelView, model=PainelVisualizacao):
    name = "Painel de Visualização"
    name_plural = "Painéis de Visualização"
    icon = "fa-solid fa-display"
    
    column_list = [PainelVisualizacao.nome, PainelVisualizacao.tipo_layout, PainelVisualizacao.andares]
    
    # Adicionando a opção que faltava na lista de 'choices'
    form_overrides = {
        'tipo_layout': SelectField,
    }
    form_args = {
        'tipo_layout': {
            'label': 'Tipo de Layout',
            'choices': [
                ('planta_unica', 'Planta Única com Pontos'),
                ('grade_quartos', 'Grade de Quartos Individuais'),
                ('multi_planta', 'Multi-Planta em Grid (ex: Bbraun)') # <-- NOME ATUALIZADO
            ]
        }
    }
    
    form_columns = [
        PainelVisualizacao.nome,
        PainelVisualizacao.slug,
        PainelVisualizacao.tipo_layout,
        PainelVisualizacao.andares
    ]

class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    can_create = False
    can_edit = False
    column_list = [ReceivedEvent.id, ReceivedEvent.data_on, ReceivedEvent.ativo, ReceivedEvent.action, ReceivedEvent.status, ReceivedEvent.rssi]
    column_searchable_list = [ReceivedEvent.ativo, ReceivedEvent.esp_id]
    column_sortable_list = [ReceivedEvent.id, ReceivedEvent.data_on]
    name = "Evento Recebido"
    name_plural = "Eventos Recebidos"
    icon = "fa-solid fa-list-ul"

# Adiciona as views ao painel de admin
admin.add_view(TipoDeAtivoAdmin) 
admin.add_view(TipoDeQuartoAdmin)
admin.add_view(AssetAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(QuartoAdmin) 
admin.add_view(AndarAdmin) 
admin.add_view(PainelAdmin)
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

def get_or_create_andar(db: Session, nome: str) -> Andar:
    andar = db.query(Andar).filter(Andar.nome == nome).first()
    if not andar:
        logger.info(f"Andar '{nome}' não encontrado. Criando novo registro.")
        andar = Andar(nome=nome)
        db.add(andar)
        db.commit()
        db.refresh(andar)
    return andar

def get_or_create_quarto(db: Session, nome: str, andar_id: int, tipo_quarto_id: Optional[int] = None) -> Quarto:
    quarto = db.query(Quarto).filter(Quarto.nome == nome, Quarto.andar_id == andar_id).first()
    if not quarto:
        logger.info(f"Quarto '{nome}' não encontrado. Criando e associando ao andar ID {andar_id}.")
        quarto = Quarto(nome=nome, andar_id=andar_id, tipo_quarto_id=tipo_quarto_id)
        db.add(quarto)
        db.commit()
        db.refresh(quarto)
    # Se o quarto já existe, mas o tipo mudou
    elif tipo_quarto_id and quarto.tipo_quarto_id != tipo_quarto_id:
        quarto.tipo_quarto_id = tipo_quarto_id
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

# @app.post("/embarcados/{embarcado_id}/reset", name="reset_esp_state")
# async def reset_esp_state(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
#     await synchronize_and_reset_esp(db=db, embarcado_id=embarcado_id)
#     time.sleep(1) 
#     return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

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
            last_rssi = reading.get("last_rssi", -1000)
            
            average_rssi = round(state.get_average_rssi(embarcado.id_esp))
        
            report_data.append({
                "mac": mac,
                "rssi": last_rssi,         # Este é o último sinal bruto visto por ESTE ESP
                "avg_rssi": average_rssi   # Esta é a média GERAL do ativo em todos os ESPs
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

@app.post("/quartos/{quarto_id}/force_cleanup", name="force_quarto_cleanup")
async def force_quarto_cleanup(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    """
    Nova ação "Reset": Força a remoção de TODOS os ativos de um quarto específico.
    Substitui a antiga `synchronize_and_reset_esp`.
    """
    assets_no_quarto = db.query(Asset).filter(Asset.quarto_id == quarto_id).all()
    if not assets_no_quarto:
        logger.info(f"Limpeza de quarto {quarto_id} solicitada, mas o quarto já está vazio.")
        return RedirectResponse(request.url_for("list_embarcados"), status_code=303)
        
    logger.warning(f"Iniciando remoção forçada de {len(assets_no_quarto)} ativo(s) do quarto ID {quarto_id}.")
    for asset in assets_no_quarto:
        await force_asset_removal(
            db=db, 
            asset_id=asset.id,
            details=f"Remoção forçada pelo operador."
        )
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

# ==============================================================================
# NOVO ENDPOINT PARA CALLBACK DE INTEGRAÇÃO
# ==============================================================================
@app.post("/api/integration/presence_callback", name="presence_callback")
async def presence_callback(request: Request, db: Session = Depends(get_db), payload: Dict = Body(...)):
    """
    Endpoint genérico para receber confirmações de presença de sistemas externos.
    """
    try:
        nome_ativo = payload.get("nome_ativo")
        status_conexao = payload.get("status")
        if not nome_ativo or not status_conexao:
            raise HTTPException(status_code=400, detail="Payload inválido.")

        logger.info(f"[CALLBACK] Mensagem recebida para o ativo: {nome_ativo} com status: {status_conexao}")
        
        asset = db.query(Asset).filter(Asset.nome_ativo == nome_ativo).first()
        if not asset:
            logger.warning(f"[CALLBACK] Ativo '{nome_ativo}' não encontrado.")
            return JSONResponse(content={"status": "Asset not found"}, status_code=404)

        # --- INÍCIO DA CORREÇÃO ---
        # Agora, a condição aceita a transição tanto de PENDENTE quanto de ALERTA para CONFIRMADO.
        if status_conexao == 'Connected' and asset.location_status in ['PENDENTE', 'ALERTA']:
        # --- FIM DA CORREÇÃO ---
            logger.info(f"[CALLBACK] Ativo '{nome_ativo}' confirmado no quarto. Atualizando status de '{asset.location_status}' para 'CONFIRMADO'.")
            change = {
                "asset_id": asset.id, "new_quarto_id": asset.quarto_id, "location_status": "CONFIRMADO",
                "details": f"Confirmação recebida via callback externo '{status_conexao}'."
            }
            await batch_update_asset_assignments(db, [change])
        
        elif status_conexao == 'Disconnected' and asset.location_status == 'CONFIRMADO':
            logger.warning(f"[CALLBACK] Ativo '{nome_ativo}' desconectado. Gerando alerta.")
            change = {
                "asset_id": asset.id, "new_quarto_id": asset.quarto_id, "location_status": "ALERTA",
                "details": "Ativo perdeu conexão de rede (callback 'Disconnected')."
            }
            await batch_update_asset_assignments(db, [change])

        db.commit()
        return JSONResponse(content={"status": "ok"})

    except Exception as e:
        logger.error(f"[CALLBACK] Erro ao processar mensagem: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro interno ao processar callback.")

async def pending_manager_task(db: Session):
    """
    Verifica ativos PENDENTES.
    1. Implementa o backoff de alerta (5m, 15m, 30m, 1h, 3h, 6h).
    2. Verifica a flag 'enable_pending_alert' para decidir se deve
       mover para ALERTA ou apenas registrar o evento no histórico.
    """
    
    # Lê a flag global de alerta do config.ini
    alert_enabled = settings.get('enable_pending_alert', 'true').lower() == 'true'

    # Define os limites de tempo do backoff em segundos
    # (5m, 15m, 30m, 1h, 3h, 6h)
    BACKOFF_THRESHOLDS_SEC = [300, 900, 1800, 3600, 10800, 21600]

    now_utc = datetime.now(timezone.utc)
    
    # Busca todos os ativos que estão no estado 'Pendente'
    pending_assets = db.query(Asset).filter(Asset.location_status == 'Pendente').all()
    if not pending_assets:
        return

    logger.info(f"[PENDING-MGR] Verificando {len(pending_assets)} ativo(s) pendente(s)... (Alertas: {'ON' if alert_enabled else 'OFF'})")

    for asset in pending_assets:
        if not asset.location_status_updated_on:
            continue

        # Calcula há quanto tempo o ativo está pendente
        time_since_pending = (now_utc - asset.location_status_updated_on.replace(tzinfo=timezone.utc)).total_seconds()

        # Conta quantos alertas de timeout já foram gerados para este ativo
        alert_count = db.query(ReceivedEvent).filter(
            ReceivedEvent.ativo == asset.mac_beacon,
            ReceivedEvent.action == "ALERTA",
            ReceivedEvent.status.like('Pendente-Timeout-%') # Conta os alertas de timeout anteriores
        ).count()

        # Verifica se já atingimos o limite máximo de alertas de backoff
        if alert_count >= len(BACKOFF_THRESHOLDS_SEC):
            continue # Já passou por todos os níveis de backoff

        # Pega o próximo limite de tempo
        current_threshold = BACKOFF_THRESHOLDS_SEC[alert_count]

        # Se o tempo pendente ultrapassou o limite atual...
        if time_since_pending > current_threshold:
            
            # Hora de gerar um evento de alerta.
            # A decisão do que fazer depende da flag global.
            
            change_to_commit = None
            
            if alert_enabled:
                # COMPORTAMENTO PADRÃO (Alertas LIGADOS)
                logger.warning(f"[PENDING-MGR] Ativo {asset.nome_ativo} excedeu o Nível {alert_count + 1} de timeout. Movendo para ALERTA.")
                change_to_commit = {
                    "asset_id": asset.id,
                    "location_status": "ALERTA", # Mova para Alerta
                    "action": "ALERTA",
                    "status": f"Pendente-Timeout-{alert_count + 1}",
                    "details": f"Ativo pendente excedeu o limite de {current_threshold}s. Alerta Nível {alert_count + 1}.",
                    "quarto_context_id": asset.quarto_id
                }
            else:
                # COMPORTAMENTO NOVO (Alertas DESLIGADOS)
                logger.info(f"[PENDING-MGR] Ativo {asset.nome_ativo} excedeu o Nível {alert_count + 1} de timeout. Logando alerta (Alertas desativados).")
                change_to_commit = {
                    "asset_id": asset.id,
                    "location_status": "PENDENTE", # Mantenha em Pendente
                    "action": "ALERTA", # A *ação* ainda é um Alerta (para o histórico)
                    "status": f"Ignorado-Timeout-{alert_count + 1}", # Status especial para o histórico
                    "details": f"Timeout de pendência Nível {alert_count + 1}. Alertas globais desativados.",
                    "quarto_context_id": asset.quarto_id
                }
            
            if change_to_commit:
                await batch_update_asset_assignments(db, [change_to_commit], aggregator._asset_map)
                db.commit()

async def main_pending_manager_loop():
    """Loop principal que executa a tarefa do gerenciador de pendências."""
    logger.info("[PENDING-MGR] Serviço de gerenciamento de pendências iniciado.")
    while True:
        # Roda a verificação a cada 30 segundos
        await asyncio.sleep(30) 
        db = SessionLocal()
        try:
            await pending_manager_task(db)
        finally:
            db.close()

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
        await asyncio.sleep(90)

def get_global_settings(db: Session) -> dict:
    settings_from_db = db.query(GlobalSetting).all()
    defaults = {"rssi_threshold": "-60", "inercia_chegada": "5000", "inercia_saida": "15000", "conflict_margin_db": "10"}
    db_settings = {s.key: s.value for s in settings_from_db}
    return {**defaults, **db_settings}

async def pending_manager_task(db: Session):
    """
    Verifica ativos no estado 'Pendente' e, se o tempo limite for excedido,
    muda seu estado para 'Alerta', gerando o evento correspondente.
    """
    pending_assets = db.query(Asset).filter(Asset.location_status == 'Pendente').all()
    if not pending_assets: return

    now_utc = datetime.now(timezone.utc)
    PENDING_EXPIRATION_TIMEOUT_SEC = int(settings.get('pending_expiration_timeout_sec', 600))

    for asset in pending_assets:
        if not asset.location_status_updated_on: continue
        time_since_pending = now_utc - asset.location_status_updated_on.replace(tzinfo=timezone.utc)

        if time_since_pending > timedelta(seconds=PENDING_EXPIRATION_TIMEOUT_SEC):
            logger.warning(f"[PENDING-MGR] Ativo {asset.nome_ativo} pendente excedeu o tempo limite. Movendo para ALERTA.")
            change_to_alert = {
                "asset_id": asset.id, "new_quarto_id": asset.quarto_id, "location_status": "ALERTA",
                "details": f"Ativo pendente não recebeu confirmação externa em {PENDING_EXPIRATION_TIMEOUT_SEC} segundos."
            }
            await batch_update_asset_assignments(db, [change_to_alert])
            db.commit()

async def main_pending_manager_loop():
    """Loop principal que executa a tarefa do gerenciador de pendências."""
    logger.info("[PENDING-MGR] Serviço de gerenciamento de pendências iniciado.")
    while True:
        await asyncio.sleep(PENDING_MANAGER_INTERVAL_SEC) 
        db = SessionLocal()
        try:
            await pending_manager_task(db)
        finally:
            db.close()

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

@app.get("/api/painel/{slug_painel}", name="get_dados_painel")
def get_dados_painel(slug_painel: str, db: Session = Depends(get_db)):
    """
    API Unificada e Final: Agora envia o 'location_status' de cada ativo.
    """
    painel = db.query(PainelVisualizacao).options(
        joinedload(PainelVisualizacao.andares)
        .joinedload(Andar.quartos)
        .joinedload(Quarto.assets), # Carrega os ativos
        joinedload(PainelVisualizacao.andares)
        .joinedload(Andar.quartos)
        .joinedload(Quarto.embarcados) # Carrega os embarcados
    ).filter(PainelVisualizacao.slug == slug_painel).first()

    if not painel:
        raise HTTPException(status_code=404, detail="Painel não encontrado")

    now_utc = datetime.now(timezone.utc)
    fuso_local = timezone(timedelta(hours=-3))
    
    andares_data = []
    for andar in painel.andares:
        quartos_data = []
        for quarto in andar.quartos:
            status_embarcado = "Offline"
            if quarto.embarcados:
                status_embarcado = quarto.embarcados[0].status_rede.capitalize()
            
            # --- LÓGICA ATUALIZADA AQUI ---
            # Para cada ativo, agora também pegamos o seu location_status.
            ativos_detalhados = []
            for asset in quarto.assets:
                ativos_detalhados.append({
                    "nome": asset.nome_ativo,
                    "status": asset.location_status  # <-- INFORMAÇÃO CRUCIAL ADICIONADA
                })
            # --- FIM DA LÓGICA ATUALIZADA ---
            
            quartos_data.append({
                "id_quarto": f"quarto-{quarto.id}",
                "nome_quarto": quarto.nome,
                "pos_x": quarto.pos_x,
                "pos_y": quarto.pos_y,
                "imagem_url": f"/static/plantas/{quarto.quarto_imagem_url}" if quarto.quarto_imagem_url else None,
                "status_embarcado": status_embarcado,
                "numero_ativos": len(ativos_detalhados),
                "ativos": ativos_detalhados # A lista agora contém o status de cada um
            })
        
        andares_data.append({
            "id_andar": andar.id,
            "nome_andar": andar.nome,
            "imagem_url": f"/static/plantas/{andar.planta_imagem_url}" if andar.planta_imagem_url else None,
            "quartos": quartos_data
        })

    return {
        "nome_painel": painel.nome,
        "tipo_layout": painel.tipo_layout,
        "andares": andares_data
    }

@app.get("/api/planta/dados", name="get_planta_dados") #a
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
    """Exibe a página de status dos quartos com o formulário de gerenciamento."""
    # 1. Busca os dados para a lista de status (como na sua versão original)
    quartos = db.query(Quarto).options(
        joinedload(Quarto.andar),
        joinedload(Quarto.tipo_de_quarto),
        joinedload(Quarto.embarcados),
        joinedload(Quarto.assets) # Garante que os ativos sejam carregados
    ).order_by(Quarto.nome).all()

    # Lógica para data de entrada dos ativos (da sua versão original)
    for quarto in quartos:
        for asset in quarto.assets:
            ultimo_evento_entrada = db.query(ReceivedEvent).filter(
                ReceivedEvent.ativo == asset.mac_beacon,
                ReceivedEvent.action == 'GET',
                ReceivedEvent.status.in_(['OK', 'Confirmado'])
            ).order_by(ReceivedEvent.data_on.desc()).first()
            if ultimo_evento_entrada:
                asset.data_entrada_str = ultimo_evento_entrada.data_on.strftime("%d/%m/%Y às %H:%M:%S")
            else:
                asset.data_entrada_str = "Não registrado"

    # 2. Busca os dados para o formulário de cadastro/edição
    all_tipos_de_quarto = db.query(TipoDeQuarto).order_by(TipoDeQuarto.nome).all()
    all_andares = db.query(Andar).order_by(Andar.nome).all()

    return templates.TemplateResponse("quartos_list.html", {
        "request": request,
        "quartos": quartos,
        "all_tipos_de_quarto": all_tipos_de_quarto,
        "all_andares": all_andares,
        "form_action": request.url_for("create_quarto"),
        "quarto": None # Para o formulário de criação
    })

@app.post("/quartos", name="create_quarto")
def create_quarto(request: Request, db: Session = Depends(get_db),
    nome: str = Form(...),
    andar_id: int = Form(...),
    tipo_quarto_id: int = Form(...)
):    
    # Verifica se já existe um quarto com o mesmo nome
    quarto_existente = db.query(Quarto).filter_by(nome=nome.strip()).first()
    if quarto_existente:
        # Lógica para lidar com erro (pode ser uma mensagem flash no futuro)
        logger.error(f"Tentativa de criar quarto com nome duplicado: {nome.strip()}")
        return RedirectResponse(request.url_for("list_quartos"), status_code=303)

    novo_quarto = Quarto(
        nome=nome.strip(),
        andar_id=andar_id,
        tipo_quarto_id=tipo_quarto_id
    )
    db.add(novo_quarto)
    db.commit()
    
    aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)

@app.get("/quartos/{quarto_id}/edit", name="edit_quarto")
def edit_quarto(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    """Exibe o formulário de edição para um quarto específico."""
    quarto_para_editar = db.query(Quarto).get(quarto_id)
    if not quarto_para_editar:
        raise HTTPException(status_code=404, detail="Quarto não encontrado")
    
    # Busca todos os dados necessários para renderizar a página completa
    quartos = db.query(Quarto).options(
        joinedload(Quarto.andar), joinedload(Quarto.tipo_de_quarto), 
        joinedload(Quarto.embarcados), joinedload(Quarto.assets)
    ).order_by(Quarto.nome).all()

    all_tipos_de_quarto = db.query(TipoDeQuarto).order_by(TipoDeQuarto.nome).all()
    all_andares = db.query(Andar).order_by(Andar.nome).all()

    return templates.TemplateResponse("quartos_list.html", {
        "request": request,
        "quartos": quartos,
        "all_tipos_de_quarto": all_tipos_de_quarto,
        "all_andares": all_andares, # Passa a lista para o template
        "form_action": request.url_for("update_quarto", quarto_id=quarto_id),
        "quarto": quarto_para_editar # Passa o objeto para preencher o form
    })

@app.post("/quartos/{quarto_id}/edit", name="update_quarto")
def update_quarto(request: Request, quarto_id: int, db: Session = Depends(get_db),
    nome: str = Form(...),
    andar_id: int = Form(...),      # CORREÇÃO: Espera o ID do andar (andar_id)
    tipo_quarto_id: int = Form(...)
):
    """Processa a atualização de um quarto existente."""
    quarto = db.query(Quarto).get(quarto_id)
    if not quarto:
        raise HTTPException(status_code=404, detail="Quarto não encontrado")
        
    quarto.nome = nome.strip()
    quarto.andar_id = andar_id      # Salva o ID diretamente
    quarto.tipo_quarto_id = tipo_quarto_id
    
    db.commit()
    aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)

@app.post("/quartos/{quarto_id}/delete", name="delete_quarto") # Garanta que é @app.post
def delete_quarto(request: Request, quarto_id: int, db: Session = Depends(get_db)):
    quarto = db.query(Quarto).get(quarto_id)
    if quarto:
        if quarto.embarcados:
            raise HTTPException(status_code=400, detail="Não é possível excluir um quarto com um embarcado associado.")
        if quarto.assets:
             raise HTTPException(status_code=400, detail="Não é possível excluir um quarto com ativos localizados nele. Remova os ativos primeiro.")

        db.delete(quarto)
        db.commit()
        aggregator.flag_for_reload()
    return RedirectResponse(request.url_for("list_quartos"), status_code=303)

# ===================================================================
# SEÇÃO X: VISUALIZAÇÃO DE PLANTAS (VERSÃO UNIFICADA FINAL)
# ===================================================================

@app.get("/api/painel/{slug_painel}", name="get_dados_painel")
def get_dados_painel(slug_painel: str, db: Session = Depends(get_db)):
    """
    API Unificada e Final: Agora envia o 'location_status' de cada ativo
    e garante que o sumário seja sempre retornado.
    """
    painel = db.query(PainelVisualizacao).options(
        joinedload(PainelVisualizacao.andares)
        .joinedload(Andar.quartos)
        .joinedload(Quarto.assets),
        joinedload(PainelVisualizacao.andares)
        .joinedload(Andar.quartos)
        .joinedload(Quarto.embarcados)
    ).filter(PainelVisualizacao.slug == slug_painel).first()

    if not painel:
        raise HTTPException(status_code=404, detail="Painel não encontrado")

    now_utc = datetime.now(timezone.utc)
    fuso_local = timezone(timedelta(hours=-3))
    
    sumario_geral = {"quartos_online": 0, "total_ativos": 0}
    
    andares_data = []
    for andar in painel.andares:
        sumario_andar = {"quartos_online": 0, "total_ativos": 0}
        
        quartos_data = []
        for quarto in andar.quartos:
            status_embarcado = "Offline"
            if quarto.embarcados and quarto.embarcados[0].last_seen:
                last_seen_utc = quarto.embarcados[0].last_seen.replace(tzinfo=timezone.utc)
                if (now_utc - last_seen_utc).total_seconds() < ESP_TIMEOUT_SEC:
                    status_embarcado = "Online"
            
            if status_embarcado == "Online":
                sumario_andar["quartos_online"] += 1
            
            ativos_detalhados = []
            for asset in quarto.assets:
                ativos_detalhados.append({
                    "nome": asset.nome_ativo,
                    "status": asset.location_status
                })
            
            sumario_andar["total_ativos"] += len(ativos_detalhados)
            
            quartos_data.append({
                "id_quarto": f"quarto-{quarto.id}", "nome_quarto": quarto.nome,
                "pos_x": quarto.pos_x, "pos_y": quarto.pos_y,
                "imagem_url": f"/static/plantas/{quarto.quarto_imagem_url}" if quarto.quarto_imagem_url else None,
                "status_embarcado": status_embarcado, "numero_ativos": len(ativos_detalhados),
                "ativos": ativos_detalhados
            })
        
        andares_data.append({
            "nome_andar": andar.nome,
            "imagem_url": f"/static/plantas/{andar.planta_imagem_url}" if andar.planta_imagem_url else None,
            "quartos": quartos_data,
            "sumario": sumario_andar
        })
        
        sumario_geral["quartos_online"] += sumario_andar["quartos_online"]
        sumario_geral["total_ativos"] += sumario_andar["total_ativos"]

    # Monta a resposta final, garantindo que o sumário esteja sempre presente.
    response_data = {
        "nome_painel": painel.nome,
        "tipo_layout": painel.tipo_layout,
    }
    if painel.tipo_layout == 'grade_quartos':
        todos_os_quartos = []
        for andar_data in andares_data:
            todos_os_quartos.extend(andar_data["quartos"])
        response_data['quartos'] = todos_os_quartos
        response_data['sumario_geral'] = sumario_geral
    else:
        response_data['andares'] = andares_data
        # Para os outros layouts, podemos também adicionar o sumário geral se for útil
        response_data['sumario_geral'] = sumario_geral

    return response_data

@app.get("/plantas", name="list_paineis")
def list_paineis(request: Request, db: Session = Depends(get_db)):
    """
    Esta rota agora lê a 'version' do config.ini e redireciona
    diretamente para o painel com o slug correspondente.
    """
    # Lê a chave 'version' da seção [Deployment]
    default_slug = settings.get("version", None)
    
    if not default_slug:
        raise HTTPException(status_code=500, detail="A chave 'version' não está definida no config.ini")
    
    # Redireciona para a URL do painel correspondente (ex: /plantas/ff)
    return RedirectResponse(url=request.url_for("view_painel", slug_painel=default_slug))

@app.get("/plantas/{slug_painel}", name="view_painel")
def view_painel(request: Request, slug_painel: str, db: Session = Depends(get_db)):
    """Renderiza a página de planta para um painel específico."""
    painel = db.query(PainelVisualizacao).filter(PainelVisualizacao.slug == slug_painel).first()
    if not painel:
        raise HTTPException(status_code=404, detail="Painel não encontrado")
    
    # Não precisamos mais passar todos os painéis, apenas o atual
    return templates.TemplateResponse("planta.html", {
        "request": request,
        "painel_atual": painel
    })

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
    all_tipos_de_quarto = db.query(TipoDeQuarto).order_by(TipoDeQuarto.nome).all()
    
    query = db.query(Embarcado).options(
        joinedload(Embarcado.quarto).joinedload(Quarto.andar),
        joinedload(Embarcado.quarto).joinedload(Quarto.tipo_de_quarto) # Carrega o tipo do quarto
    )
    
    # Lógica de busca
    if search:
        search_term = f"%{search}%"
        query = query.join(Embarcado.quarto).join(Quarto.andar).filter(
            or_(Embarcado.id_esp.ilike(search_term), Quarto.nome.ilike(search_term), Andar.nome.ilike(search_term), Embarcado.mac_address.ilike(search_term), Embarcado.ip_address.ilike(search_term))
        )
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
    
    assigned_quarto_ids = {emb.quarto_id for emb in embarcados if emb.quarto_id is not None}
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_quarto_ids)).order_by(Quarto.nome).all()

    global_settings = get_global_settings(db)
    rssi_thresholds = {"global": int(global_settings.get("rssi_threshold", -60)),"individuais": {emb.id_esp: emb.rssi_threshold for emb in embarcados if emb.rssi_threshold is not None}}
    fuso_local = timezone(timedelta(hours=-3))
    for emb in embarcados:
        emb.status = emb.status_rede.capitalize() if emb.status_rede else "Desconhecido"
        if emb.last_seen:
            last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
            data_local = last_seen_utc.astimezone(fuso_local)
            emb.last_seen_str = data_local.strftime("às %H:%M:%S de %d/%m")
        else:
            emb.last_seen_str = "Nunca visto"
    
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "available_quartos": available_quartos,
        "form_action": request.url_for("create_embarcado"),
        "embarcado": None, 
        "search": search,
        "global_settings": get_global_settings(db),
        "rssi_thresholds": json.dumps(rssi_thresholds),
        "all_tipos_de_quarto": all_tipos_de_quarto, # <-- ADICIONADO: Passa a lista para o template
        "current_filters": {"search": search, "sort_by": sort_by, "order": order}
    })

@app.post("/embarcados/new", name="create_embarcado")
def create_embarcado(request: Request, db: Session = Depends(get_db),
    id_esp: str = Form(...),
    quarto_id: int = Form(...), # Recebe o ID do quarto diretamente do dropdown
    rssi_threshold: Optional[str] = Form(None),
    connecta_id: Optional[str] = Form(None)
):
    # A lógica de get_or_create_quarto não é mais necessária aqui.
    rssi_value = int(rssi_threshold) if rssi_threshold else None
    
    novo_embarcado = Embarcado(
        id_esp=id_esp, 
        quarto_id=quarto_id, # Associa diretamente o ID
        rssi_threshold=rssi_value, 
        connecta_id=connecta_id
    )
    
    try:
        db.add(novo_embarcado)
        db.commit()
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
    """Exibe o formulário de edição para um embarcado, garantindo que a lista de quartos disponíveis esteja correta."""
    emb_para_editar = db.query(Embarcado).get(embarcado_id)
    if not emb_para_editar:
        raise HTTPException(status_code=404, detail="Embarcado não encontrado")

    # Lógica para popular o dropdown de quartos:
    # Pega os IDs de todos os quartos que estão atribuídos a OUTROS embarcados.
    assigned_to_others_ids = {
        emb.quarto_id for emb in db.query(Embarcado).filter(
            Embarcado.id != embarcado_id, # Exclui o embarcado atual da verificação
            Embarcado.quarto_id.isnot(None)
        ).all()
    }
    # A lista de quartos disponíveis são todos os quartos que NÃO estão na lista acima.
    available_quartos = db.query(Quarto).filter(Quarto.id.notin_(assigned_to_others_ids)).order_by(Quarto.nome).all()

    # Busca todos os embarcados para exibir a lista de fundo
    embarcados = db.query(Embarcado).options(joinedload(Embarcado.quarto).joinedload(Quarto.andar)).order_by(Embarcado.id_esp).all()

    # Formata a data para a lista de fundo
    fuso_local = timezone(timedelta(hours=-3))
    for emb in embarcados:
        emb.status = emb.status_rede.capitalize() if emb.status_rede else "Desconhecido"
        if emb.last_seen:
            last_seen_utc = emb.last_seen.replace(tzinfo=timezone.utc)
            data_local = last_seen_utc.astimezone(fuso_local)
            emb.last_seen_str = data_local.strftime("às %H:%M:%S de %d/%m")
        else:
            emb.last_seen_str = "Nunca visto"

    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": emb_para_editar, # O objeto que está sendo editado
        "available_quartos": available_quartos, # Passa a lista correta para o form de edição
        "global_settings": get_global_settings(db),
        "current_filters": {"search": None, "sort_by": "id_esp", "order": "asc"}
    })

# Em main.py

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(
    request: Request,
    embarcado_id: int,
    db: Session = Depends(get_db),
    # CAMPOS ATUALIZADOS PARA CORRESPONDER AO NOVO FORMULÁRIO
    quarto_id: int = Form(...),
    rssi_threshold: Optional[str] = Form(None),
    connecta_id: Optional[str] = Form(None)
):
    """Processa a atualização de um embarcado existente."""
    emb = db.query(Embarcado).get(embarcado_id)
    if emb:
        rssi_value = int(rssi_threshold) if rssi_threshold and rssi_threshold.strip() != '' else None
        
        # LÓGICA SIMPLIFICADA
        emb.quarto_id = quarto_id
        emb.rssi_threshold = rssi_value
        emb.connecta_id = connecta_id
        
        db.commit()
        aggregator.flag_for_reload()
        logger.info(f"[main] Embarcado '{emb.id_esp}' atualizado.")
        
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
@app.get("/ativos", name="list_assets")
def list_assets(request: Request, db: Session = Depends(get_db), search: Optional[str] = None, sort_by: str = "nome_ativo", order: str = "asc"):
    # ATUALIZADO: Carrega a lista de Tipos de Ativo para passar para o formulário
    all_tipos_de_ativo = db.query(TipoDeAtivo).order_by(TipoDeAtivo.nome).all()

    query = db.query(Asset).options(
        joinedload(Asset.quarto),
        joinedload(Asset.tipo_de_ativo) # Carrega o tipo do ativo
    )
    
    if search:
        search_term = f"%{search}%"
        query = query.outerjoin(Asset.quarto).outerjoin(Asset.tipo_de_ativo).filter(
            or_(
                Asset.nome_ativo.ilike(search_term), 
                Asset.mac_beacon.ilike(search_term), 
                Quarto.nome.ilike(search_term), 
                TipoDeAtivo.nome.ilike(search_term) # Busca pelo nome do tipo
            )
        )
    
    # ATUALIZADO: Corrige a ordenação e adiciona a ordenação pelo nome do tipo
    sortable_columns = {
        "nome_ativo": Asset.nome_ativo, 
        "tipo_ativo": TipoDeAtivo.nome, # Ordena pelo nome do tipo
        "mac_beacon": Asset.mac_beacon, 
        "quarto": Quarto.nome
    }
    if sort_by == "quarto":
        query = query.outerjoin(Asset.quarto)
    if sort_by == "tipo_ativo":
        query = query.outerjoin(Asset.tipo_de_ativo)
        
    sort_column = sortable_columns.get(sort_by, Asset.nome_ativo)
    query = query.order_by(asc(sort_column) if order == "asc" else desc(sort_column))

    assets = query.all()
    
    return templates.TemplateResponse("assets_list.html", {
        "request": request, "assets": assets,
        "form_action": request.url_for("create_asset"), "asset": None, 
        "all_tipos_de_ativo": all_tipos_de_ativo, # <-- ADICIONADO: Passa a lista para o template
        "current_filters": {"search": search, "sort_by": sort_by, "order": order}
    })


@app.post("/ativos", name="create_asset")
def create_asset(request: Request, db: Session = Depends(get_db),
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    tipo_ativo_id: int = Form(...), # <-- ADICIONADO: Recebe a ID do tipo
    mac_address: Optional[str] = Form(None),
    modelo: Optional[str] = Form(None),
    fabricante: Optional[str] = Form(None)
):
    asset = Asset(
        nome_ativo=nome_ativo,
        mac_beacon=mac_beacon.lower(),
        tipo_ativo_id=tipo_ativo_id, # <-- ATUALIZADO: Salva a ID do tipo
        mac_address=mac_address.lower() if mac_address else None,
        modelo=modelo,
        fabricante=fabricante
    )
    try:
        db.add(asset)
        db.commit()
        aggregator.flag_for_reload()
        # ... (lógica de notificação MQTT)
    except IntegrityError:
        db.rollback()
        logger.error(f"[main-db] ERRO: Tentativa de criar ativo com nome ou MAC duplicado.")
    except Exception as e:
        db.rollback()
        logger.error(f"[main-db] ERRO ao criar ativo: {e}")
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.get("/ativos/{asset_id}/edit", name="edit_asset")
def edit_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    # CORREÇÃO: Busca a lista de tipos de ativo para popular o formulário de edição.
    all_tipos_de_ativo = db.query(TipoDeAtivo).order_by(TipoDeAtivo.nome).all()
    
    return templates.TemplateResponse("assets_list.html", {
        "request": request, 
        "assets": db.query(Asset).order_by(Asset.nome_ativo).all(),
        "form_action": request.url_for("update_asset", asset_id=asset_id),
        "asset": db.query(Asset).get(asset_id), 
        "all_tipos_de_ativo": all_tipos_de_ativo, # Passa a lista para o template
        "current_filters": {"search": None, "sort_by": "nome_ativo", "order": "asc"}
    })

@app.post("/ativos/{asset_id}/edit", name="update_asset")
def update_asset(
    request: Request, asset_id: int, db: Session = Depends(get_db),
    # CORREÇÃO: Parâmetros alinhados com o formulário final.
    nome_ativo: str = Form(...),
    mac_beacon: str = Form(...),
    tipo_ativo_id: int = Form(...),
    mac_address: Optional[str] = Form(None),
    modelo: Optional[str] = Form(None),
    fabricante: Optional[str] = Form(None)
):
    asset = db.query(Asset).get(asset_id)
    if asset:
        mac_antigo = asset.mac_beacon
        mac_novo = mac_beacon.lower()

        if mac_antigo != mac_novo:
            aggregator.clear_asset_state(mac_antigo)

        # CORREÇÃO: Salva os dados nos campos corretos do modelo.
        asset.nome_ativo = nome_ativo
        asset.mac_beacon = mac_novo
        asset.tipo_ativo_id = tipo_ativo_id # Salva o ID do tipo
        asset.mac_address = mac_address.lower() if mac_address else None
        asset.modelo = modelo
        asset.fabricante = fabricante
        
        db.commit()
        aggregator.flag_for_reload() 
        # (A notificação MQTT já está correta)
            
    return RedirectResponse(request.url_for("list_assets"), status_code=303)

@app.post("/ativos/{asset_id}/delete", name="delete_asset") # Mude de @app.get para @app.post
def delete_asset(request: Request, asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).get(asset_id)
    if asset:
        mac_para_limpar = asset.mac_beacon
        aggregator.clear_asset_state(mac_para_limpar)
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
@app.get("/eventos", name="list_events")
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

        if e.data_on:
            data_utc = e.data_on.replace(tzinfo=timezone.utc)
            data_local = data_utc.astimezone(sao_paulo_tz)
            e.data_str = data_local.strftime("%d/%m/%Y")
            e.hora_str = data_local.strftime("%H:%M:%S")

        tooltip_parts = []
        if e.status:
            tooltip_parts.append(f"Status: {e.status}")
        if e.status_detail:
            tooltip_parts.append(f"Detalhes: {e.status_detail}")
        
        # Junta as partes com um separador. Se não houver status ou detalhe, o texto será vazio.
        e.tooltip_text = " | ".join(tooltip_parts) #

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": total > page * EVENT_PAGE_SIZE,
        "all_assets": db.query(Asset.nome_ativo, Asset.mac_beacon).distinct().order_by(Asset.nome_ativo).all(),
        "all_action_options": [("GET", "Conectar"), ("OUT", "Desconectar")],
        "all_status_options": ["OK", "Erro", "Enfileirado", "Ignorado", "Confirmado"],
        "all_quartos": sorted([q.nome for q in db.query(Quarto).order_by(Quarto.nome).all()]),
        "current_filters": {"ativo": filter_ativo, "quarto": filter_quarto, "action": filter_action, "status": filter_status, "time_filter": time_filter}
    })

@app.get("/eventos/download", name="download_events_csv")
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

@app.get("/ativos/download", name="download_assets_csv")
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

async def bed_state_processor_loop():
    """
    Processa mensagens de status da cama (connected/disconnected) vindas da fila MQTT.
    Este é o novo "callback" que substitui o endpoint HL7.
    """
    logger.info("[BED_PROCESSOR] Processador de Status de Cama iniciado.")

    # Lê a flag global de alerta do config.ini
    alert_enabled = settings.get('enable_pending_alert', 'true').lower() == 'true'
    if alert_enabled:
        logger.info("[BED_PROCESSOR] Modo de Alerta de Desconexão: ATIVADO.")
    else:
        logger.info("[BED_PROCESSOR] Modo de Alerta de Desconexão: DESATIVADO (desconexões irão para PENDENTE).")

    while True:
        try:
            # Espera por uma nova mensagem na fila
            payload = await bed_state_queue.get()

            nome_cama = payload.get("id")
            is_connected = payload.get("connected")

            if nome_cama is None or is_connected is None:
                logger.warning(f"[BED_PROCESSOR] Payload de status de cama inválido recebido: {payload}")
                continue

            db = SessionLocal()
            try:
                # Encontra o ativo pelo nome (que é o campo "id" no JSON da cama)
                asset = db.query(Asset).filter(Asset.nome_ativo == nome_cama).first()

                if not asset:
                    logger.warning(f"[BED_PROCESSOR] Status recebido para cama '{nome_cama}', mas ela não foi encontrada no DB.")
                    continue

                change_to_commit = None # Prepara a "ordem de mudança"

                # --- LÓGICA DE MUDANÇA DE ESTADO ---

                if is_connected:
                    # Se o ativo estava Pendente OU Alertado, ele agora é Confirmado.
                    if asset.location_status in ['PENDENTE', 'ALERTA']:
                        logger.info(f"[BED_PROCESSOR] Ativo '{nome_cama}' (de {asset.location_status}) foi CONFIRMADO via MQTT.")
                        change_to_commit = {
                            "asset_id": asset.id,
                            "new_quarto_id": asset.quarto_id, # Mantém o quarto que já estava
                            "location_status": "CONFIRMADO", # O novo estado final
                            "action": "GET",
                            "status": "Confirmado",
                            "details": "Entrada confirmada via callback MQTT 'connected: true'.",
                            "quarto_context_id": asset.quarto_id
                        }

                else: # Se is_connected == false
                    if asset.location_status == 'CONFIRMADO':
                        
                        if alert_enabled:
                            # COMPORTAMENTO PADRÃO (Alertas LIGADOS)
                            logger.warning(f"[BED_PROCESSOR] Ativo '{nome_cama}' desconectado. Gerando ALERTA.")
                            change_to_commit = {
                                "asset_id": asset.id,
                                "location_status": "ALERTA", # Mova para Alerta
                                "action": "ALERTA",
                                "status": "Ativo",
                                "details": f"Ativo perdeu conexão de rede (Callback MQTT 'connected: false'). Razão: {payload.get('disconnectreason', 'N/A')}",
                                "quarto_context_id": asset.quarto_id
                            }
                        else:
                            # COMPORTAMENTO NOVO (Alertas DESLIGADOS)
                            logger.info(f"[BED_PROCESSOR] Ativo '{nome_cama}' desconectado. Movendo para PENDENTE (Alertas desativados).")
                            change_to_commit = {
                                "asset_id": asset.id,
                                "location_status": "PENDENTE", # Mova para Pendente
                                "action": "ALERTA", # A *ação* ainda é um Alerta (para o histórico)
                                "status": "Ignorado-Desconexao", # Um status especial para o histórico
                                "details": f"Desconexão de cama. Alertas globais desativados, movido para pendente.",
                                "quarto_context_id": asset.quarto_id
                            }

                # Se uma mudança foi decidida, chama o "Executor"
                if change_to_commit:
                    # Passa o _asset_map do aggregator para a função de serviço
                    await batch_update_asset_assignments(db, [change_to_commit], aggregator._asset_map)
                    db.commit()
                    await manager.broadcast("ATUALIZAR_ESTADO") # Notifica o frontend

            finally:
                db.close()
        except Exception as e:
            logger.error(f"[BED_PROCESSOR] Erro crítico no loop do processador de camas: {e}", exc_info=True)
            # Adiciona um pequeno delay para evitar loops de erro muito rápidos
            await asyncio.sleep(5)

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

@app.on_event("startup")
async def on_startup():
    running_tasks = {}
    logger.info("[STARTUP] Iniciando serviços em background.")
    running_tasks["aggregator"] = asyncio.create_task(main_aggregator_loop())
    running_tasks["liveness_check"] = asyncio.create_task(check_esp_liveness())
    running_tasks["esp_status_updater"] = asyncio.create_task(batch_update_esp_status())
    running_tasks["pending_manager"] = asyncio.create_task(main_pending_manager_loop())
    running_tasks["bed_state_processor"] = asyncio.create_task(bed_state_processor_loop())
    # NOVA TAREFA ADICIONADA
    running_tasks["pending_manager"] = asyncio.create_task(main_pending_manager_loop())
    
    mqtt_client.start_mqtt_client()
    bed_mqtt_client.start_bed_client()
    start_cleanup_scheduler()
    logger.info("[STARTUP] Startup concluído. Enviando comando de sincronização para todas as ESPs.")    
    command_payload = {"command": "fetch_config"} 
    mqtt_client.client.publish(topic=settings.get("mqtt_esp_command_topic"), payload=json.dumps(command_payload), qos=1)

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app", host=settings.get("ip", "0.0.0.0"),
        port=int(settings.get("port", 8000)), reload=True
    )