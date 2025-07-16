# main.py

import asyncio
import threading
import time
import uvicorn

from fastapi import FastAPI, Request, Response, Form, HTTPException, Body, Query, Depends
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional, Dict
from datetime import datetime, timedelta, timezone
import csv
from io import StringIO
import json

# MUDANÇA: Importa Badge em vez de Bed
from .models import (
    engine,
    SessionLocal,
    Badge,
    Embarcado,
    ReceivedEvent,
    GlobalSetting,
    init_db
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# MUDANÇA: O import de cancel_pending_task é removido
from .aggregator import main_aggregator_loop, enqueue_event
from . import mqtt_client
# MUDANÇA: Funções de serviço e MQTT são renomeadas para refletir 'badge'
from .services import update_badge_assignment, trigger_mqtt_update_on_badge_change
from sqlalchemy import event, or_
from sqlalchemy.orm import Session
from .mqtt_client import publish_available_badges
from .auth import authenticate_admin
from .config import (
    HISTORY_RETENTION_DAYS,
    EVENT_PAGE_SIZE,
    CLEANUP_INTERVAL_SEC,
    IP,
    MQTT_ESP_COMMAND_TOPIC
)
from sqladmin import Admin, ModelView

print("[main] Módulo carregado para a versão CRACHÁS.")

# inicializa banco
init_db()
app = FastAPI()

# --- Autenticação do Admin (sem mudanças) ---
security = HTTPBasic()

@app.middleware("http")
async def protect_admin_routes(request: Request, call_next):
    if request.url.path.startswith("/admin"):
        try:
            creds: HTTPBasicCredentials = await security(request)
            authenticate_admin(creds)
        except HTTPException as exc:
            return Response(content=exc.detail, status_code=exc.status_code, headers=exc.headers)
    return await call_next(request)

# MUDANÇA: Listener agora observa o modelo Badge
def structural_badge_change_listener(mapper, connection, target):
    trigger_mqtt_update_on_badge_change()

event.listen(Badge, 'after_insert', structural_badge_change_listener)
event.listen(Badge, 'after_delete', structural_badge_change_listener)

# --- Configuração do SQLAdmin ---
admin_app = FastAPI()
admin = Admin(admin_app, engine, base_url="/")

def get_global_settings(db: Session) -> dict:
    settings = db.query(GlobalSetting).all()
    # Define valores padrão caso não existam no banco
    defaults = {
        "rssi_threshold": "-60",
        "inercia_chegada": "500",
        "inercia_saida": "15000"
    }
    # Converte a lista de objetos para um dicionário
    db_settings = {s.key: s.value for s in settings}
    # Junta os valores do banco com os padrões (banco tem precedência)
    return {**defaults, **db_settings}

# MUDANÇA: BedAdmin para BadgeAdmin
class BadgeAdmin(ModelView, model=Badge):
    column_list = [Badge.id, Badge.nome_cracha, Badge.mac_beacon, Badge.quarto]
    column_searchable_list = [Badge.nome_cracha, Badge.mac_beacon, Badge.quarto]
    name = "Crachá"
    name_plural = "Crachás"
    icon = "fa-solid fa-id-badge"

# (Sem mudanças no EmbarcadoAdmin e ReceivedEventAdmin)
class EmbarcadoAdmin(ModelView, model=Embarcado):
    # --- ATUALIZE A LISTA DE COLUNAS ---
    column_list = [
        Embarcado.id, 
        Embarcado.id_esp, 
        Embarcado.quarto, 
    ]
    column_searchable_list = [Embarcado.id_esp, Embarcado.quarto]
    name = "Embarcado"
    name_plural = "Embarcados"
    icon = "fa-solid fa-microchip"

class ReceivedEventAdmin(ModelView, model=ReceivedEvent):
    column_list = [ReceivedEvent.id, ReceivedEvent.data_on, ReceivedEvent.cracha, ReceivedEvent.action, ReceivedEvent.status, ReceivedEvent.esp_id]
    column_default_sort = ('data_on', True)
    column_searchable_list = [ReceivedEvent.cracha, ReceivedEvent.esp_id, ReceivedEvent.status]
    can_create = False
    can_edit = False
    name = "Evento"
    name_plural = "Eventos"
    icon = "fa-solid fa-clock-rotate-left"

admin.add_view(BadgeAdmin)
admin.add_view(EmbarcadoAdmin)
admin.add_view(ReceivedEventAdmin)

app.mount("/admin", admin_app)

# --- Estáticos e Templates ---
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

# ==========================================================
# ENDPOINT DE EVENTOS (Recepção sem mudanças funcionais)
# ==========================================================
@app.post("/event")
async def receive_event(event_data: Dict):
    print(f"[main] Evento HTTP recebido: {event_data}")

    required_keys = ["esp_id", "cracha", "status"]
    if not all(key in event_data for key in required_keys):
        raise HTTPException(status_code=400, detail="Payload incompleto.")

    db = SessionLocal()
    try:
        db_event = ReceivedEvent(
            esp_id=event_data.get("esp_id"),
            cracha=event_data.get("cracha"),
            action=event_data.get("status"),
            status="Enfileirado",
            status_detail="Aguardando processamento pelo agregador simplificado",
            rssi=event_data.get("RSSI"),
            wifi=event_data.get("wifi"),
            data_on=datetime.fromisoformat(event_data.get("data_on").replace("Z", "+00:00")),
            raw=event_data
        )
        db.add(db_event)
        db.commit()
        db.refresh(db_event)

        event_with_id = {**event_data, "event_id": db_event.id}
        enqueue_event(event_with_id)

        return {"status": "success", "message": "Evento recebido e enfileirado"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao processar e salvar o evento: {e}")
    finally:
        db.close()

# ==========================================================
# LISTA DE EVENTOS (LÓGICA SIMPLIFICADA)
# ==========================================================
@app.get("/events", name="list_events")
def list_events(
    request: Request,
    page: int = 1,
    filter_cracha: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_action: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_andar: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    embarcados_map = {emb.id_esp: {"quarto": emb.quarto, "andar": emb.andar} for emb in db.query(Embarcado).all()}
    beacon_to_badge_name_map = {b.mac_beacon: b.nome_cracha for b in db.query(Badge).filter(Badge.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)

    if filter_cracha:
        query = query.filter(ReceivedEvent.cracha == filter_cracha)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
    if filter_quarto:
        esps = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
        query = query.filter(ReceivedEvent.esp_id.in_(esps)) if esps else query.filter(False)
    if filter_action:
        query = query.filter(ReceivedEvent.action == filter_action)
    if filter_status:
        query = query.filter(ReceivedEvent.status == filter_status)
    if filter_andar:
        esps = [id_esp for id_esp, data in embarcados_map.items() if data["andar"] == filter_andar]
        query = query.filter(ReceivedEvent.esp_id.in_(esps)) if esps else query.filter(False)

    total = query.count()
    events = (
        query.order_by(ReceivedEvent.data_on.desc())
             .offset((page - 1) * EVENT_PAGE_SIZE)
             .limit(EVENT_PAGE_SIZE)
             .all()
    )
    has_next = total > page * EVENT_PAGE_SIZE

    all_badges = db.query(Badge.nome_cracha, Badge.mac_beacon).filter(Badge.mac_beacon.isnot(None)).distinct().order_by(Badge.nome_cracha).all()
    all_action_options = [("GET", "Conectar"), ("OUT", "Desconectar")]
    all_status_options = ["OK", "Erro", "Enfileirado", "Ignorado", "Confirmado"]
    all_andares = sorted([str(a[0]) for a in db.query(Embarcado.andar).distinct().filter(Embarcado.andar.isnot(None)).all()])
    all_quartos = sorted([str(q[0]) for q in db.query(Embarcado.quarto).distinct().filter(Embarcado.quarto.isnot(None)).all()])

    for e in events:
        e.data_str = e.data_on.strftime("%Y/%m/%d") if e.data_on else "N/A"
        e.hora_str = e.data_on.strftime("%H:%M:%S") if e.data_on else "N/A"
        emb_data = embarcados_map.get(e.esp_id)
        e.quarto = emb_data.get("quarto", "---") if emb_data else "---"
        e.andar = emb_data.get("andar", "---") if emb_data else "---"
        e.nome_cracha = beacon_to_badge_name_map.get(e.cracha, e.cracha)

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": events, "page": page, "has_next": has_next,
        "all_badges": all_badges, "all_action_options": all_action_options,
        "all_status_options": all_status_options, "all_andares": all_andares,
        "all_quartos": all_quartos,
        "current_filters": {
            "cracha": filter_cracha, "quarto": filter_quarto, "action": filter_action,
            "status": filter_status, "andar": filter_andar, "time_filter": time_filter
        }
    })

@app.post("/settings/update", name="update_settings")
def update_settings(
    request: Request, db: Session = Depends(get_db),
    rssi_threshold: str = Form(...),
    inercia_chegada: str = Form(...),
    inercia_saida: str = Form(...)
):
    settings_data = {
        "rssi_threshold": rssi_threshold,
        "inercia_chegada": inercia_chegada,
        "inercia_saida": inercia_saida
    }
    for key, value in settings_data.items():
        setting = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
        if not setting:
            setting = GlobalSetting(key=key)
            db.add(setting)
        setting.value = value
    db.commit()

    # --- AÇÃO ADICIONADA: Publicar o comando de atualização ---
    print("[main] Configurações globais salvas. Enviando comando de atualização para todas as ESPs.")
    command_payload = {"command": "fetch_config"}
    mqtt_client.client.publish(
        MQTT_ESP_COMMAND_TOPIC,
        json.dumps(command_payload)
    )
    # -----------------------------------------------------------

    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

# ==========================================================
#     ROTAS DE DOWNLOAD E STARTUP (com pequenas atualizações)
# ==========================================================
@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    db: Session = Depends(get_db),
    filter_cracha: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_andar: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None)
):
    # A lógica interna é muito parecida com a da rota 'list_events'
    embarcados_map = {emb.id_esp: {"quarto": emb.quarto, "andar": emb.andar} for emb in db.query(Embarcado).all()}
    beacon_to_badge_name_map = {b.mac_beacon: b.nome_cracha for b in db.query(Badge).filter(Badge.mac_beacon.isnot(None)).all()}

    query = db.query(ReceivedEvent)

    # Aplica os mesmos filtros da página de eventos
    if filter_cracha:
        query = query.filter(ReceivedEvent.cracha == filter_cracha)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=1))
        elif time_filter == 'weekly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(weeks=1))
        elif time_filter == 'monthly': query = query.filter(ReceivedEvent.data_on >= now - timedelta(days=30))
    if filter_quarto:
        esps = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
        query = query.filter(ReceivedEvent.esp_id.in_(esps)) if esps else query.filter(False)
    if filter_status:
        query = query.filter(ReceivedEvent.status == filter_status)
    if filter_andar:
        esps = [id_esp for id_esp, data in embarcados_map.items() if data["andar"] == filter_andar]
        query = query.filter(ReceivedEvent.esp_id.in_(esps)) if esps else query.filter(False)

    events = query.order_by(ReceivedEvent.data_on.desc()).all()

    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)

        # Cabeçalho do CSV atualizado para "Crachá"
        writer.writerow(["Data/Hora", "Nome do Crachá", "Andar", "Quarto", "Status", "Ação", "RSSI", "Wi-Fi"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        for e in events:
            emb_data = embarcados_map.get(e.esp_id, {})
            quarto = emb_data.get("quarto", "")
            andar = emb_data.get("andar", "")
            nome_cracha = beacon_to_badge_name_map.get(e.cracha, e.cracha)

            writer.writerow([
                e.data_on.strftime("%Y-%m-%d %H:%M:%S") if e.data_on else "",
                nome_cracha,
                andar,
                quarto,
                e.status,
                e.action,
                e.rssi,
                e.wifi
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

@app.get("/badges/download", name="download_badges_csv")
def download_badges_csv(db: Session = Depends(get_db)):
    badges = db.query(Badge).order_by(Badge.nome_cracha).all()
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["NOME DO CRACHÁ", "MAC BEACON", "QUARTO ATUAL"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for badge in badges:
            writer.writerow([badge.nome_cracha, badge.mac_beacon, badge.quarto or ""])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=crachas_export.csv"})

@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv(db: Session = Depends(get_db)):
    # (sem mudanças)
    embarcados = db.query(Embarcado).order_by(Embarcado.quarto).all()
    def iter_csv():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID DO EMBARCADO", "QUARTO", "ANDAR"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for emb in embarcados:
            writer.writerow([emb.id_esp, emb.quarto, emb.andar or ""])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    return StreamingResponse(iter_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"})


def purge_old_events():
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
        deleted = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
        db.commit()
        print(f"[main] Limpeza de eventos antigos: {deleted} removidos.")
    finally:
        db.close()

def start_cleanup_scheduler():
    def loop():
        while True:
            time.sleep(CLEANUP_INTERVAL_SEC)
            purge_old_events()
    threading.Thread(target=loop, daemon=True).start()

@app.on_event("startup")
async def on_startup():
    print("[main] Startup: Iniciando agregador, cliente MQTT e agendador de limpeza.")
    asyncio.create_task(main_aggregator_loop())
    mqtt_client.connect_mqtt()
    start_cleanup_scheduler()

# ==========================================================
#     PÁGINA PRINCIPAL E CRUD CRACHÁS
# ==========================================================
@app.get("/", name="main")
def main_page(request: Request):
    return RedirectResponse(url=request.url_for("list_events"))

@app.get("/badges", name="list_badges")
def list_badges(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Badge)
    if search:
        search_term = f"%{search}%"
        query = query.filter(or_(Badge.nome_cracha.ilike(search_term), Badge.mac_beacon.ilike(search_term), Badge.quarto.ilike(search_term)))
    badges = query.order_by(Badge.nome_cracha).all()
    return templates.TemplateResponse("badges_list.html", {
        "request": request, "badges": badges,
        "form_action": request.url_for("create_badge"),
        "badge": None, "search": search
    })

@app.post("/badges", name="create_badge")
def create_badge(request: Request, nome_cracha: str = Form(...), mac_beacon: str = Form(...), db: Session = Depends(get_db)):
    badge = Badge(nome_cracha=nome_cracha, mac_beacon=mac_beacon.lower())
    db.add(badge)
    db.commit()

    trigger_mqtt_update_on_badge_change()

    return RedirectResponse(request.url_for("list_badges"), status_code=303)

@app.get("/badges/{badge_id}/edit", name="edit_badge")
def edit_badge(request: Request, badge_id: int, db: Session = Depends(get_db)):
    badge = db.query(Badge).get(badge_id)
    badges = db.query(Badge).order_by(Badge.nome_cracha).all()
    return templates.TemplateResponse("badges_list.html", {
        "request": request, "badges": badges,
        "form_action": request.url_for("update_badge", badge_id=badge_id),
        "badge": badge
    })

@app.post("/badges/{badge_id}/edit", name="update_badge")
def update_badge(
    request: Request, badge_id: int,
    nome_cracha: str = Form(...),
    mac_beacon: str = Form(...),
    db: Session = Depends(get_db)
):
    badge = db.query(Badge).get(badge_id)
    if not badge:
        raise HTTPException(status_code=404, detail="Crachá não encontrado")

    mac_mudou = badge.mac_beacon != mac_beacon.lower()

    badge.nome_cracha = nome_cracha
    badge.mac_beacon = mac_beacon.lower()
    
    db.commit()

    if mac_mudou:
        trigger_mqtt_update_on_badge_change()

    return RedirectResponse(request.url_for("list_badges"), status_code=303)

@app.get("/badges/{badge_id}/delete", name="delete_badge")
def delete_badge(request: Request, badge_id: int, db: Session = Depends(get_db)):
    badge = db.query(Badge).get(badge_id)
    db.delete(badge)
    db.commit()
    trigger_mqtt_update_on_badge_change()
    return RedirectResponse(request.url_for("list_badges"), status_code=303)

@app.get("/esp/{esp_id}/config", name="get_config")
def get_config_for_esp(esp_id: str, db: Session = Depends(get_db)):
    """
    Verifica o estado inicial da ESP, retornando o crachá no seu quarto
    E as configurações de sensibilidade personalizadas para ela.
    """
    embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    if not embarcado:
        return {"mac_beacon": None}

    badge = db.query(Badge).filter(Badge.quarto == embarcado.quarto).first()
    
    # Busca as configurações globais para enviar à ESP
    settings = get_global_settings(db)
    
    response_data = {
        "mac_beacon": badge.mac_beacon if badge else None,
        # Converte para os tipos corretos (int)
        "rssi_threshold": int(settings.get("rssi_threshold")),
        "inercia_chegada": int(settings.get("inercia_chegada")),
        "inercia_saida": int(settings.get("inercia_saida")),
    }
    return response_data

# ==========================================================
#     CRUD EMBARCADOS (sem mudanças)
# ==========================================================
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request, search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Embarcado)
    if search:
        search_term = f"%{search}%"
        query = query.filter(or_(Embarcado.id_esp.ilike(search_term), Embarcado.quarto.ilike(search_term), Embarcado.andar.ilike(search_term)))
    
    embarcados = query.order_by(Embarcado.quarto).all()
    global_settings = get_global_settings(db) # Pega as configurações globais

    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("create_embarcado"),
        "embarcado": None,
        "search": search,
        "global_settings": global_settings # Passa as configs para o template
    })

@app.post("/embarcados", name="create_embarcado")
def create_embarcado(
    request: Request, db: Session = Depends(get_db),
    id_esp: str = Form(...),
    quarto: str = Form(...),
    andar: Optional[str] = Form(None),
):
    emb = Embarcado(
        id_esp=id_esp,
        quarto=quarto,
        andar=andar,
        
    )
    db.add(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


@app.get("/embarcados/{embarcado_id}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    embarcados = db.query(Embarcado).order_by(Embarcado.quarto).all()
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request, "embarcados": embarcados,
        "form_action": request.url_for("update_embarcado", embarcado_id=embarcado_id),
        "embarcado": emb
    })

@app.post("/embarcados/{embarcado_id}/edit", name="update_embarcado")
def update_embarcado(
    request: Request, embarcado_id: int, db: Session = Depends(get_db),
    quarto: str = Form(...),
    andar: Optional[str] = Form(None),
):
    emb = db.query(Embarcado).get(embarcado_id)
    if not emb:
        raise HTTPException(status_code=404, detail="Embarcado não encontrado")
    
    emb.quarto = quarto
    emb.andar = andar
    
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)


@app.get("/embarcados/{embarcado_id}/delete", name="delete_embarcado")
def delete_embarcado(request: Request, embarcado_id: int, db: Session = Depends(get_db)):
    emb = db.query(Embarcado).get(embarcado_id)
    db.delete(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

# ==========================================================
#     EXECUÇÃO
# ==========================================================
if __name__ == "__main__":
    uvicorn.run("app.main:app", host=IP, port=8000, reload=True)