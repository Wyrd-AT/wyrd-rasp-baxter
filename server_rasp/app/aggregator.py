import asyncio
import time 
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from .dispatcher import dispatch_event
from .models import SessionLocal, Bed, Embarcado, ReceivedEvent
from .config import settings
from .presence import check_presence
from .mqtt_client import publish_available_beds, publish_to_esp_channel, publish_command_to_all
from .services import synchronize_and_reset_esp

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

async def _retry_presence_task(wifi_mac: str, beacon_mac: str, original_event_id: int, esp_id: str):
    """
    Tarefa em segundo plano que verifica a presença.
    Se a ausência persistir, gera um WARNING e eventualmente expira.
    """
    print(f"[aggregator-retry] Iniciada tarefa para Beacon '{beacon_mac}' (MAC Wi-Fi: {wifi_mac}).")
    
    start_time = datetime.now(timezone.utc)
    warning_created = False
    warning_delay_minutes = int(settings.get('warning_delay_minutes', 5))
    
    # LÓGICA DO STATUS "VENCIDO"
    # Define um tempo máximo que uma tarefa pode ficar pendente.
    max_pending_minutes = 15 

    while True:
        if datetime.now(timezone.utc) - start_time > timedelta(minutes=max_pending_minutes):
            print(f"[aggregator-retry] MAC '{wifi_mac}' pendente por mais de {max_pending_minutes} min. Marcando como Vencido.")
            _update_event_status(
                original_event_id, 
                status="Vencido", 
                detail=f"A presença do Wi-Fi não foi confirmada em {max_pending_minutes} minutos."
            )
            
            # ================== AÇÃO DE SINCRONIZAÇÃO ==================
            # Chama a função de serviço para desassociar a cama e resetar a ESP.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, synchronize_and_reset_esp, esp_id)
            # =========================================================

            break # Encerra a tarefa.

        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        
        loop = asyncio.get_running_loop()
        is_present = await loop.run_in_executor(None, check_presence, wifi_mac)

        if is_present:
            print(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac}' encontrado na rede.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                if bed:
                    _update_event_status(
                        original_event_id, 
                        status="Resolvido", 
                        detail=f"Cama '{bed.nome_cama}' associada e confirmada no quarto '{bed.quarto}' via nova tentativa."
                    )
                    dispatch_payload = {
                        "quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET",
                        "dataOn": datetime.now(timezone.utc).isoformat(),
                    }
                    await loop.run_in_executor(None, dispatch_event, dispatch_payload)
                break 
            finally:
                db.close()
        
        elif not warning_created:
            elapsed_time = datetime.now(timezone.utc) - start_time
            if elapsed_time > timedelta(minutes=warning_delay_minutes):
                print(f"[aggregator-retry] MAC '{wifi_mac}' ausente por mais de {warning_delay_minutes} min. GERANDO WARNING.")
                db = SessionLocal()
                try:
                    bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                    emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
                    
                    if bed and emb:
                        timestamp_agora = datetime.now(timezone.utc)
                        warning_event = ReceivedEvent(
                            esp_id=esp_id, cama=beacon_mac,
                            action="WARNING", status="OK",
                            status_detail=f"Alerta gerado para cama '{bed.nome_cama}' com MAC ausente por mais de {warning_delay_minutes} min.",
                            data_on=timestamp_agora,
                            raw={"reason": "Auto-generated by aggregator", "original_event_id": original_event_id}
                        )
                        db.add(warning_event)
                        db.commit()
                        warning_payload = {
                            "quarto": emb.quarto, "cama": bed.nome_cama, 
                            "status": "WARNING", "dataOn": timestamp_agora.isoformat()
                        }
                        await loop.run_in_executor(None, dispatch_event, warning_payload)
                        warning_created = True
                finally:
                    db.close()

    if wifi_mac in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac]
    print(f"[aggregator-retry] Finalizada tarefa de verificação para MAC Wi-Fi '{wifi_mac}'.")

async def _process_events_batch(events: list):
    loop = asyncio.get_running_loop()
    db = SessionLocal()
    
    try:
        out_events = [e for e in events if e.get("status") == "OUT"]
        if out_events:
            main_out_event = out_events[0]
            event_id = main_out_event.get("event_id")
            beacon_mac = main_out_event.get("cama")
            print(f"[aggregator] Evento 'OUT' detectado para o beacon '{beacon_mac}'.")
            
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
                    _update_event_status(event_id, "Confirmado", "Cama já estava desassociada.")
            else:
                _update_event_status(event_id, "Erro", f"Cama com beacon '{beacon_mac}' não cadastrada.")
            return

        get_events = [e for e in events if e.get("status") == "GET"]
        if not get_events:
            return

        best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
        event_id = best_event.get("event_id")
        beacon_mac = best_event.get("cama")
        
        # ADIÇÃO: Extrai o ID da transação do evento vencedor.
        transacao_id = best_event.get("transacao_id")

        # ATUALIZAÇÃO: Envia o veredito WIN incluindo o ID da transação.
        print(f"[aggregator-verdict] Enviando WIN (ID: {transacao_id}) para a ESP {best_event['esp_id']}")
        publish_to_esp_channel(
            esp_id=best_event['esp_id'],
            message_type="verdict",
            data={"cama": beacon_mac, "status": "WIN", "transacao_id": transacao_id}
        )

        # ATUALIZAÇÃO: Envia o veredito LOSE incluindo o ID da transação.
        for evt in get_events:
            if evt is not best_event:
                _update_event_status(evt.get("event_id"), "Ignorado", f"Sinal mais fraco. Veredito: LOSE.")
                print(f"[aggregator-verdict] Enviando LOSE (ID: {transacao_id}) para a ESP {evt['esp_id']}")
                publish_to_esp_channel(
                    esp_id=evt['esp_id'],
                    message_type="verdict",
                    data={"cama": beacon_mac, "status": "LOSE", "transacao_id": transacao_id}
                )
        
        bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == best_event['esp_id']).first()

        if not bed or not emb:
            _update_event_status(event_id, "Erro", "Componente (cama ou ESP) não cadastrado.")
            return

        if bed.quarto == emb.quarto:
            _update_event_status(event_id, "Confirmado", f"Cama '{bed.nome_cama}' já estava no quarto '{emb.quarto}'.")
            return
        
        bed.quarto = emb.quarto
        db.commit()
        
        is_present = await loop.run_in_executor(None, check_presence, bed.mac_address)
        
        if is_present:
            publish_available_beds()
            dispatch_payload = {"quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET", "dataOn": datetime.now(timezone.utc).isoformat(), "wifi": best_event.get("wifi")}
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)
            _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' associada e confirmada no quarto '{emb.quarto}'.")
        else:
            _update_event_status(event_id, "Pendente", f"Cama associada ao quarto '{emb.quarto}', aguardando confirmação do Wi-Fi.")
            if bed.mac_address not in _pending_mac_checks:
                task = asyncio.create_task(
                    _retry_presence_task(
                        wifi_mac=bed.mac_address, 
                        beacon_mac=beacon_mac, 
                        original_event_id=event_id,
                        esp_id=best_event['esp_id']
                    )
                )
                _pending_mac_checks[bed.mac_address] = task
    finally:
        db.close()

def cancel_pending_task(wifi_mac: str) -> bool:
    if wifi_mac in _pending_mac_checks:
        task = _pending_mac_checks[wifi_mac]
        task.cancel()
        del _pending_mac_checks[wifi_mac]
        print(f"[aggregator-cancel] Tarefa para o MAC {wifi_mac} foi cancelada externamente.")
        return True
    
    print(f"[aggregator-cancel] Nenhuma tarefa pendente encontrada para o MAC {wifi_mac}.")
    return False

async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print(f"[aggregator] Agregador com lógica de status iniciada. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")
    
    # LÓGICA DO HEARTBEAT
    last_heartbeat_time = time.time()
    heartbeat_interval_sec = 3600
    print(f"[aggregator] Heartbeat configurado para cada {heartbeat_interval_sec} segundos.")
    
    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)
        
        # Envia o log de "sinal de vida".
        if time.time() - last_heartbeat_time > heartbeat_interval_sec:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Aggregator Heartbeat: Processo ativo.")
            last_heartbeat_time = time.time()

        if not _buffer:
            continue

        events_to_process = list(_buffer)
        _buffer.clear()

        events_by_beacon = defaultdict(list)
        for evt in events_to_process:
            if "cama" in evt:
                events_by_beacon[evt["cama"]].append(evt)
        
        print(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        for beacon_mac, events in events_by_beacon.items():
            await _process_events_batch(events)