import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from .dispatcher import dispatch_event
from .models import SessionLocal, Bed, Embarcado, ReceivedEvent
from .presence import check_presence
from .mqtt_client import publish_available_beds

# --- Configurações ---
RETRY_PRESENCE_FREQUENCY_SEC = 30
AGGREGATOR_LOOP_INTERVAL_SEC = 2

# --- Estruturas de Dados em Memória ---
_buffer = []
_pending_mac_checks = {}

def enqueue_event(evt: dict):
    """ Coloca um novo evento no buffer para ser processado. """
    _buffer.append(evt)
    print(f"[aggregator] Evento enfileirado: {evt}")

# --- NOVA FUNÇÃO HELPER ---
def _update_event_status(event_id: int, status: str, detail: str):
    """ Pequena função para atualizar o status de um evento no banco de dados. """
    db = SessionLocal()
    try:
        event_to_update = db.query(ReceivedEvent).filter(ReceivedEvent.id == event_id).first()
        if event_to_update:
            event_to_update.status = status
            event_to_update.status_detail = detail
            db.commit()
    except Exception as e:
        print(f"[aggregator-update] ERRO ao atualizar evento {event_id}: {e}")
        db.rollback()
    finally:
        db.close()

# --- LÓGICA DE RETRY ATUALIZADA ---
async def _retry_presence_task(wifi_mac: str, beacon_mac: str, original_event: dict):
    """
    Tarefa em segundo plano que tenta verificar a presença de um MAC Wi-Fi.
    """
    original_event_id = original_event.get("event_id")
    print(f"[aggregator-retry] Iniciada tarefa para Beacon '{beacon_mac}' (MAC Wi-Fi: {wifi_mac}). Evento ID: {original_event_id}.")
    
    while True:
        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        
        loop = asyncio.get_running_loop()
        is_present = await loop.run_in_executor(None, check_presence, wifi_mac)

        if is_present:
            print(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac}' encontrado na rede.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                emb = db.query(Embarcado).filter(Embarcado.id_esp == original_event["esp_id"]).first()

                if emb and bed and bed.quarto is None:
                    bed.quarto = emb.quarto
                    db.commit()
                    
                    publish_available_beds()

                    dispatch_payload = {
                        "quarto": bed.quarto,
                        "cama": bed.nome_cama,
                        "status": "IN",
                        "dataOn": datetime.now(timezone.utc).isoformat(),
                        "wifi": original_event.get("wifi")
                    }
                    await loop.run_in_executor(None, dispatch_event, dispatch_payload)

                    _update_event_status(
                        original_event_id, 
                        status="OK", 
                        detail=f"Cama '{bed.nome_cama}' associada ao quarto '{emb.quarto}' via nova tentativa."
                    )
                break 
            finally:
                db.close()
    
    if wifi_mac in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac]
    print(f"[aggregator-retry] Finalizada tarefa de verificação para MAC Wi-Fi '{wifi_mac}'.")

# --- LÓGICA DE PROCESSAMENTO PRINCIPAL TOTALMENTE REFEITA ---
async def _process_events_batch(events: list):
    """
    Processa um lote de eventos para um mesmo beacon.
    """
    loop = asyncio.get_running_loop()
    db = SessionLocal()
    
    try:
        # Lógica de 'OUT' Corrigida
        out_events = [e for e in events if e.get("status") == "OUT"]
        if out_events:
            # Processa o primeiro evento 'OUT' e marca os outros como ignorados
            main_out_event = out_events[0]
            event_id = main_out_event.get("event_id")
            beacon_mac = main_out_event.get("cama")
            print(f"[aggregator] Evento 'OUT' detectado para o beacon '{beacon_mac}'.")
            
            # Marca os outros eventos OUT como ignorados
            for evt in out_events[1:]:
                _update_event_status(evt.get("event_id"), "Ignorado", "Evento OUT duplicado no mesmo lote.")

            bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
            if bed:
                if bed.mac_address in _pending_mac_checks:
                    _pending_mac_checks[bed.mac_address].cancel()
                    del _pending_mac_checks[bed.mac_address]
                
                if bed.quarto is not None:
                    print(f"[aggregator] Desassociando cama '{bed.nome_cama}' do quarto '{bed.quarto}'.")
                    bed.quarto = None
                    db.commit()
                    publish_available_beds()
                    _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' desassociada com sucesso.")
                else:
                    _update_event_status(event_id, "OK", "Cama já estava desassociada. Nenhuma ação necessária.")
            else:
                _update_event_status(event_id, "Erro", f"Cama com beacon '{beacon_mac}' não cadastrada.")
            return

        # Lógica de 'GET' Corrigida
        get_events = [e for e in events if e.get("status") == "GET"]
        if not get_events:
            return

        best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
        event_id = best_event.get("event_id")
        beacon_mac = best_event.get("cama")

        # Itera sobre os eventos que NÃO são os melhores e os marca como ignorados
        for evt in get_events:
            if evt is not best_event:
                _update_event_status(evt.get("event_id"), "Ignorado", f"Sinal mais fraco (RSSI: {evt.get('RSSI', 'N/A')}) ou duplicado.")
        
        bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == best_event['esp_id']).first()

        if not bed or not emb:
            detail = f"Cama (beacon: {beacon_mac})" if not bed else f"ESP ({best_event['esp_id']})"
            _update_event_status(event_id, "Erro", f"Componente não cadastrado: {detail}.")
            return

        if bed.quarto == emb.quarto:
            _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' já estava no quarto '{emb.quarto}'.")
            return
        
        is_present = await loop.run_in_executor(None, check_presence, bed.mac_address)
        
        if is_present:
            bed.quarto = emb.quarto
            db.commit()
            publish_available_beds()
            
            dispatch_payload = {"quarto": bed.quarto, "cama": bed.nome_cama, "status": "IN", "dataOn": datetime.now(timezone.utc).isoformat(), "wifi": best_event.get("wifi")}
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)
            
            _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' associada ao quarto '{emb.quarto}'.")
        else:
            # MAC não encontrado: Status Pendente + Cria e Despacha WARNING
            _update_event_status(event_id, "Pendente", "MAC da cama não encontrado. Nova verificação agendada.")

            # Cria o novo evento de WARNING no banco
            warning_event = ReceivedEvent(
                esp_id=best_event.get("esp_id"), cama=best_event.get("cama"),
                action="WARNING", status="OK",
                status_detail=f"Alerta gerado para cama '{bed.nome_cama}' com MAC ausente.",
                data_on=datetime.now(timezone.utc), raw={"reason": "Auto-generated by aggregator"}
            )
            db.add(warning_event)
            db.commit()

            # Despacha o WARNING para o Connecta
            warning_payload = {"quarto": emb.quarto, "cama": bed.nome_cama, "status": "WARNING", "dataOn": datetime.now(timezone.utc).isoformat()}
            await loop.run_in_executor(None, dispatch_event, warning_payload)
            
            if bed.mac_address not in _pending_mac_checks:
                task = asyncio.create_task(_retry_presence_task(bed.mac_address, beacon_mac, best_event))
                _pending_mac_checks[bed.mac_address] = task
    finally:
        db.close()

async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print(f"[aggregator] Agregador com lógica de status iniciada. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")
    
    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)
        
        if not _buffer:
            continue

        events_to_process = list(_buffer)
        _buffer.clear()

        events_by_beacon = defaultdict(list)
        for evt in events_to_process:
            # Ignora eventos sem o campo 'cama'
            if "cama" in evt:
                events_by_beacon[evt["cama"]].append(evt)
        
        print(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        for beacon_mac, events in events_by_beacon.items():
            await _process_events_batch(events)