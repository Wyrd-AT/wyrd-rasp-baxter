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

from .models import (
    engine,
    SessionLocal,
    Bed,
    Embarcado,
    ReceivedEvent,
    init_db
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from .presence import check_presence
from .aggregator import main_aggregator_loop, enqueue_event 
from . import mqtt_client # <-- Importe o novo módulo
from .services import update_bed_assignment, trigger_mqtt_update_on_bed_change
from sqlalchemy import event, or_ # <--- NOVO IMPORT
from sqlalchemy.orm import Session
from .mqtt_client import publish_available_beds
from .auth import authenticate_admin
from .config import (
    HISTORY_RETENTION_DAYS,
    EVENT_PAGE_SIZE,
    CLEANUP_INTERVAL_SEC, IP
)
from sqladmin import Admin, ModelView

print("[main] Módulo carregado")

# inicializa banco
init_db()
app = FastAPI()

# protege /admin com HTTP Basic
security = HTTPBasic()

@app.middleware("http")
async def protect_admin_routes(request: Request, call_next):
    if request.url.path.startswith("/admin"):
        try:
            creds: HTTPBasicCredentials = await security(request)
            authenticate_admin(creds)
        except HTTPException as exc:
            return Response(
                content=exc.detail,
                status_code=exc.status_code,
                headers=exc.headers
            )
    return await call_next(request)

def structural_bed_change_listener(mapper, connection, target):
    trigger_mqtt_update_on_bed_change()

event.listen(Bed, 'after_insert', structural_bed_change_listener)
event.listen(Bed, 'after_delete', structural_bed_change_listener)

# sub-app do SQLAdmin
admin_app = FastAPI()
admin = Admin(admin_app, engine, base_url="/")

class BedAdmin(ModelView, model=Bed):
    column_list = [Bed.id, Bed.mac_address, Bed.nome_cama, Bed.mac_beacon, Bed.quarto]
    column_searchable_list = [Bed.mac_address, Bed.nome_cama, Bed.mac_beacon, Bed.quarto]
    page_size = 20

class EmbarcadoAdmin(ModelView, model=Embarcado):
    column_list = [Embarcado.id, Embarcado.id_esp, Embarcado.quarto]
    column_searchable_list = [Embarcado.id_esp, Embarcado.quarto]
    page_size = 20

admin.add_view(BedAdmin)
admin.add_view(EmbarcadoAdmin)
app.mount("/admin", admin_app)

# estáticos e templates
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

def validate_bed_data(data: dict):
    if "cama" not in data or "quarto" not in data or "status" not in data:
        raise HTTPException(status_code=400, detail="Dados da cama incompletos.")

# ==========================================================
# NOVO ENDPOINT HTTP PARA RECEBER EVENTOS DOS ESPs
# ==========================================================
@app.post("/event")
async def receive_event(event_data: Dict):
    """
    Recebe um evento de um ESP32 via HTTP POST.
    """
    print(f"[main] Evento HTTP recebido: {event_data}")

    # Validação básica para garantir que os campos essenciais estão presentes
    required_keys = ["esp_id", "cama", "status"]
    if not all(key in event_data for key in required_keys):
        raise HTTPException(status_code=400, detail="Payload incompleto. Faltando chaves essenciais.")

    # Salva o evento bruto no banco de dados para histórico
    db = SessionLocal()
    try:
        db_event = ReceivedEvent(
            esp_id=event_data.get("esp_id"),
            cama=event_data.get("cama"),
            status=event_data.get("status"),
            rssi=event_data.get("RSSI"),
            wifi=event_data.get("wifi"),
            data_on=datetime.fromisoformat(event_data.get("data_on").replace("Z", "+00:00")),
            raw=event_data
        )
        db.add(db_event)
        db.commit()
    except Exception as e:
        print(f"[main] Erro ao salvar evento no DB: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao processar e salvar o evento.")
    finally:
        db.close()

    # Enfileira o evento para o agregador processar
    print(event_data)
    enqueue_event(event_data)
    
    return {"status": "success", "message": "Evento recebido e enfileirado"}

# lista eventos, usando data_on como timestamp principal
@app.get("/events", name="list_events")
def list_events(
    request: Request,
    page: int = 1,
    filter_cama: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_andar: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    # ... (toda a lógica de mapas e filtros continua exatamente igual) ...
    embarcados_map = {emb.id_esp: {"quarto": emb.quarto, "andar": emb.andar} for emb in db.query(Embarcado).all()}
    beacon_to_bed_name_map = {bed.mac_beacon: bed.nome_cama for bed in db.query(Bed).filter(Bed.mac_beacon.isnot(None)).all()}
    query = db.query(ReceivedEvent)

    # ... (filtros) ...
    if filter_cama:
        query = query.filter(ReceivedEvent.cama == filter_cama)
    if time_filter:
        now = datetime.now(timezone.utc)
        if time_filter == 'daily':
            start_date = now - timedelta(days=1)
            query = query.filter(ReceivedEvent.data_on >= start_date)
        elif time_filter == 'weekly':
            start_date = now - timedelta(weeks=1)
            query = query.filter(ReceivedEvent.data_on >= start_date)
        elif time_filter == 'monthly':
            start_date = now - timedelta(days=30)
            query = query.filter(ReceivedEvent.data_on >= start_date)
    if filter_quarto:
        esps_no_quarto = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
        query = query.filter(ReceivedEvent.esp_id.in_(esps_no_quarto)) if esps_no_quarto else query.filter(False)
    if filter_status:
        query = query.filter(ReceivedEvent.status == filter_status)
    if filter_andar:
        esps_no_andar = [id_esp for id_esp, data in embarcados_map.items() if data["andar"] == filter_andar]
        query = query.filter(ReceivedEvent.esp_id.in_(esps_no_andar)) if esps_no_andar else query.filter(False)

    total = query.count()
    evts = (
        query.order_by(ReceivedEvent.data_on.desc())
             .offset((page - 1) * EVENT_PAGE_SIZE)
             .limit(EVENT_PAGE_SIZE)
             .all()
    )
    has_next = total > page * EVENT_PAGE_SIZE

    all_beds = db.query(Bed.nome_cama, Bed.mac_beacon).filter(Bed.mac_beacon.isnot(None)).distinct().order_by(Bed.nome_cama).all()
    all_status_options = [("GET", "conectou"), ("OUT", "desconectou"), ("WARNING", "erro de wifi")]
    all_andares = sorted([str(a[0]) for a in db.query(Embarcado.andar).distinct().filter(Embarcado.andar.isnot(None)).all()])
    all_quartos = sorted([str(q[0]) for q in db.query(Embarcado.quarto).distinct().filter(Embarcado.quarto.isnot(None)).all()])

    # [CORREÇÃO APLICADA AQUI]
    # Enriquecimento dos dados do evento para o template
    for e in evts:
        # A conversão de fuso foi totalmente removida.
        # Agora formatamos a data/hora diretamente do que vem do banco de dados.
        e.data_str = e.data_on.strftime("%Y / %m / %d") if e.data_on else "N/A"
        e.hora_str = e.data_on.strftime("%H : %M : %S") if e.data_on else "N/A"
        
        # O resto do loop para obter quarto, andar e nome da cama continua igual
        emb_data = embarcados_map.get(e.esp_id)
        if emb_data:
            e.quarto = emb_data.get("quarto", "---")
            e.andar = emb_data.get("andar", "---")
        else:
            e.quarto, e.andar = "---", "---"

        e.nome_cama = beacon_to_bed_name_map.get(e.cama, e.cama)

    return templates.TemplateResponse("events_list.html", {
        "request": request, "events": evts, "page": page, "has_next": has_next,
        "all_beds": all_beds, "all_status_options": all_status_options, "all_andares": all_andares,
        "all_quartos": all_quartos,
        "current_filters": {
            "cama": filter_cama, "quarto": filter_quarto, "status": filter_status,
            "andar": filter_andar, "time_filter": time_filter
        }
    })

# ==========================================================
# ROTA PARA DOWNLOAD CSV DE CAMAS
# ==========================================================
@app.get("/beds/download", name="download_beds_csv")
def download_beds_csv():
    db = SessionLocal()
    try:
        beds = db.query(Bed).order_by(Bed.nome_cama).all()

        def iter_csv():
            buf = StringIO()
            writer = csv.writer(buf)

            # Cabeçalho
            writer.writerow(["MAC", "NOME", "QUARTO", "BEACON"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

            for bed in beds:
                writer.writerow([
                    bed.mac_address,
                    bed.nome_cama,
                    bed.quarto or "",
                    bed.mac_beacon or ""
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
    finally:
        db.close()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=camas_export.csv"}
    )

# ==========================================================
# ROTA PARA DOWNLOAD CSV DE EMBARCADOS
# ==========================================================
@app.get("/embarcados/download", name="download_embarcados_csv")
def download_embarcados_csv():
    db = SessionLocal()
    try:
        embarcados = db.query(Embarcado).order_by(Embarcado.quarto).all()

        def iter_csv():
            buf = StringIO()
            writer = csv.writer(buf)

            # Cabeçalho
            writer.writerow(["ID da ESP", "QUARTO"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

            for emb in embarcados:
                writer.writerow([
                    emb.id_esp,
                    emb.quarto
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
    finally:
        db.close()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=embarcados_export.csv"}
    )

# rota para download CSV
@app.get("/events/download", name="download_events_csv")
def download_events_csv(
    # 1. A função agora aceita os mesmos parâmetros de filtro
    db: Session = Depends(get_db),
    filter_cama: Optional[str] = Query(None),
    filter_quarto: Optional[str] = Query(None),
    filter_status: Optional[str] = Query(None),
    filter_andar: Optional[str] = Query(None),
    time_filter: Optional[str] = Query(None)
):
    try:
        # 2. Copiamos a mesma lógica de mapas da list_events
        embarcados_map = {emb.id_esp: {"quarto": emb.quarto, "andar": emb.andar} for emb in db.query(Embarcado).all()}
        beacon_to_bed_name_map = {bed.mac_beacon: bed.nome_cama for bed in db.query(Bed).filter(Bed.mac_beacon.isnot(None)).all()}

        # 3. Construímos a query com os filtros, exatamente como em list_events
        query = db.query(ReceivedEvent)

        if filter_cama:
            query = query.filter(ReceivedEvent.cama == filter_cama)
        if time_filter:
            now = datetime.now(timezone.utc)
            if time_filter == 'daily':
                start_date = now - timedelta(days=1)
                query = query.filter(ReceivedEvent.data_on >= start_date)
            elif time_filter == 'weekly':
                start_date = now - timedelta(weeks=1)
                query = query.filter(ReceivedEvent.data_on >= start_date)
            elif time_filter == 'monthly':
                start_date = now - timedelta(days=30)
                query = query.filter(ReceivedEvent.data_on >= start_date)
        if filter_quarto:
            esps_no_quarto = [id_esp for id_esp, data in embarcados_map.items() if data["quarto"] and filter_quarto.lower() in data["quarto"].lower()]
            query = query.filter(ReceivedEvent.esp_id.in_(esps_no_quarto)) if esps_no_quarto else query.filter(False)
        if filter_status:
            query = query.filter(ReceivedEvent.status == filter_status)
        if filter_andar:
            esps_no_andar = [id_esp for id_esp, data in embarcados_map.items() if data["andar"] == filter_andar]
            query = query.filter(ReceivedEvent.esp_id.in_(esps_no_andar)) if esps_no_andar else query.filter(False)

        # A busca agora é feita na query já filtrada
        events = query.order_by(ReceivedEvent.data_on).all()

        def iter_csv():
            buf = StringIO()
            writer = csv.writer(buf)

            # 4. Atualizamos o cabeçalho do CSV
            writer.writerow(["Data/Hora", "Nome da Cama", "Andar", "Quarto", "Status", "RSSI", "Wi-Fi"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

            for e in events:
                # 5. Buscamos os dados enriquecidos para cada linha
                emb_data = embarcados_map.get(e.esp_id, {})
                quarto = emb_data.get("quarto", "")
                andar = emb_data.get("andar", "")
                nome_cama = beacon_to_bed_name_map.get(e.cama, e.cama)

                writer.writerow([
                    e.data_on.strftime("%Y-%m-%d %H:%M:%S") if e.data_on else "",
                    nome_cama,
                    andar,
                    quarto,
                    e.status,
                    e.rssi,
                    e.wifi
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
    finally:
        db.close()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_filtrados.csv"}
    )

# limpeza periódica usando data_on
def purge_old_events():
    db = SessionLocal()
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    deleted = db.query(ReceivedEvent).filter(ReceivedEvent.data_on < cutoff).delete()
    db.commit()
    print(f"[main] purge_old_events: removidos {deleted} eventos antes de {cutoff.isoformat()}")

def start_cleanup_scheduler():
    print(f"[main] Cleanup scheduler iniciado (a cada {CLEANUP_INTERVAL_SEC}s)")
    def loop():
        while True:
            purge_old_events()
            time.sleep(CLEANUP_INTERVAL_SEC)
    threading.Thread(target=loop, daemon=True).start()

@app.on_event("startup")
async def on_startup():
    print("[main] Startup: agregador, servidor TCP e cleanup")
    asyncio.create_task(main_aggregator_loop())
    mqtt_client.connect_mqtt() 
    #asyncio.create_task(start_server())
    start_cleanup_scheduler()

@app.get("/", name="main")
def main(request: Request):
    return templates.TemplateResponse("main.html", {"request": request})

# ─── CRUD CAMAS ────────────────────────────────────────────────────────────────
@app.get("/beds", name="list_beds")
def list_beds(request: Request, search: Optional[str] = Query(None)):
    db = SessionLocal()
    
    query = db.query(Bed)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Bed.nome_cama.ilike(search_term),
                Bed.mac_address.ilike(search_term),
                Bed.quarto.ilike(search_term)
            )
        )
    
    beds = query.all()
    
    return templates.TemplateResponse("beds_list.html", {
        "request": request,
        "beds": beds,
        "form_action": request.url_for("create_bed"),
        "bed": None,
        "search": search # Envia o termo de pesquisa
    })

@app.post("/beds", name="create_bed")
def create_bed(
    request: Request,
    mac_address: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form("Nenhum")
):
    db = SessionLocal()
    bed = Bed(mac_address=mac_address, nome_cama=nome, mac_beacon=mac_beacon)
    db.add(bed)
    db.commit()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/edit", name="edit_bed")
def edit_bed(request: Request, bed_id: int):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    beds = db.query(Bed).all()
    return templates.TemplateResponse("beds_list.html", {
        "request": request,
        "beds": beds,
        "form_action": request.url_for("update_bed", bed_id=bed_id),
        "bed": bed
    })

@app.post("/beds/{bed_id}/edit", name="update_bed")
def update_bed(
    request: Request,
    bed_id: int,
    mac_address: str = Form(...),
    nome: str = Form(...),
    mac_beacon: Optional[str] = Form(None),
    quarto: Optional[str] = Form(None)
):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    bed.mac_address = mac_address
    bed.nome_cama = nome
    
    if bed.mac_beacon != mac_beacon:
        bed.mac_beacon = mac_beacon
        trigger_mqtt_update_on_bed_change()

    db.commit()
    db.close()

    update_bed_assignment(bed_id=bed_id, new_room=quarto)
    
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

@app.get("/beds/{bed_id}/delete", name="delete_bed")
def delete_bed(request: Request, bed_id: int):
    db = SessionLocal()
    bed = db.query(Bed).get(bed_id)
    db.delete(bed)
    db.commit()
    return RedirectResponse(request.url_for("list_beds"), status_code=303)

# ─── CRUD Embarcados (HTML) ────────────────────────────────────────────────────
@app.get("/embarcados", name="list_embarcados")
def list_embarcados(request: Request, search: Optional[str] = Query(None)):
    db = SessionLocal()
    
    query = db.query(Embarcado)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Embarcado.id_esp.ilike(search_term),
                Embarcado.quarto.ilike(search_term)
            )
        )
        
    embarcados = query.all()

    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("create_embarcado_html"),
        "embarcado": None,
        "search": search
    })

@app.post("/embarcados", name="create_embarcado_html")
def create_embarcado_html(
    request: Request,
    id_esp: str = Form(...),
    quarto: str = Form(...),
    andar: Optional[str] = Form(None) # <-- NOVO PARÂMETRO
):
    db = SessionLocal()
    emb = Embarcado(id_esp=id_esp, quarto=quarto, andar=andar) # <-- SALVANDO O NOVO CAMPO
    db.add(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/edit", name="edit_embarcado")
def edit_embarcado(request: Request, id_esp: str):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    embarcados = db.query(Embarcado).all()
    return templates.TemplateResponse("embarcados_list.html", {
        "request": request,
        "embarcados": embarcados,
        "form_action": request.url_for("update_embarcado", id_esp=id_esp),
        "embarcado": emb
    })

@app.post("/embarcados/{id_esp}/edit", name="update_embarcado")
def update_embarcado_html(
    request: Request,
    id_esp: str,
    quarto: str = Form(...),
    andar: Optional[str] = Form(None) # <-- NOVO PARÂMETRO
):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    emb.quarto = quarto
    emb.andar = andar # <-- ATUALIZANDO O NOVO CAMPO
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

@app.get("/embarcados/{id_esp}/delete", name="delete_embarcado")
def delete_embarcado_html(request: Request, id_esp: str):
    db = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == id_esp).first()
    db.delete(emb)
    db.commit()
    return RedirectResponse(request.url_for("list_embarcados"), status_code=303)

# ─── ROTA PARA RECEBER O JSON (com informações da cama) ───────────────────────
@app.post("/update_bed_from_json")
async def update_bed_from_json(data: dict = Body(...)):
    validate_bed_data(data)

    cama_mac = data.get("cama")
    quarto = data.get("quarto")
    status = data.get("status")

    if not check_presence(cama_mac):
        raise HTTPException(status_code=404, detail=f"Cama com MAC {cama_mac} não está conectada à rede")

    db = SessionLocal()
    bed = db.query(Bed).filter(Bed.mac_address == cama_mac).first()
    db.close()
    
    if not bed:
        raise HTTPException(status_code=404, detail=f"Cama com MAC {cama_mac} não encontrada no banco de dados.")
    
    # Usamos o serviço para garantir a atualização e publicação corretas
    new_room = quarto if status == "IN" else None
    update_bed_assignment(bed_id=bed.id, new_room=new_room)
    
    return {"message": "Cama atualizada com sucesso", "cama": cama_mac, "status": status, "quarto": new_room}

# ─── EXECUÇÃO DIRETA ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app.main:app", host=IP, port=8000, reload=True)
