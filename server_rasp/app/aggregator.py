# aggregator.py
# Este é o módulo central de processamento de eventos do sistema Baxter.

import asyncio
import time 
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Importa os módulos necessários para interagir com o resto do sistema.
from .dispatcher import dispatch_event
from .models import SessionLocal, Bed, Embarcado, ReceivedEvent
from .config import settings
from .presence import check_presence
from .mqtt_client import publish_available_beds, publish_to_esp_channel
from .services import synchronize_and_reset_esp

# --- Seção: Configurações e Estruturas de Dados em Memória ---
RETRY_PRESENCE_FREQUENCY_SEC = 30 # A cada quantos segundos tentar verificar a presença de um Wi-Fi ausente.
AGGREGATOR_LOOP_INTERVAL_SEC = 2  # Frequência do loop principal do agregador.

_buffer = [] # Fila para novos eventos.
_pending_mac_checks = {} # Dicionário de tarefas de verificação de presença em andamento.

def enqueue_event(evt: dict):
    """ Coloca um novo evento no buffer para ser processado pelo loop principal. """
    _buffer.append(evt)
    print(f"[aggregator] Evento enfileirado: {evt}")

def _update_event_status(event_id: int, status: str, detail: str):
    """ Função utilitária para atualizar o status de um evento no banco de dados. """
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

# --- Seção: Tarefa de Verificação de Presença (Retry Task) ---
# Esta tarefa assíncrona é disparada quando uma cama é detectada por um ESP,
# mas o seu módulo Wi-Fi ainda não está visível na rede. Ela fica tentando
# encontrar o Wi-Fi em intervalos regulares.
async def _retry_presence_task(wifi_mac: str, beacon_mac: str, original_event_id: int, esp_id: str):
    print(f"[aggregator-retry] Iniciada tarefa para Beacon '{beacon_mac}' (MAC Wi-Fi: {wifi_mac}).")
    
    start_time = datetime.now(timezone.utc)
    warning_created = False
    warning_delay_minutes = int(settings.get('warning_delay_minutes', 5))
    max_pending_minutes = 15 

    while True:
        # Lógica de Timeout (Vencido): Se a verificação demorar mais que o tempo máximo...
        if datetime.now(timezone.utc) - start_time > timedelta(minutes=max_pending_minutes):
            print(f"[aggregator-retry] MAC '{wifi_mac}' pendente por mais de {max_pending_minutes} min. Marcando como Vencido.")
            # Atualiza o evento original para "Vencido"
            _update_event_status(
                original_event_id, 
                status="Vencido", 
                detail=f"A presença do Wi-Fi não foi confirmada em {max_pending_minutes} minutos."
            )
            # Ação Drástica: Chama o serviço para desassociar a cama e resetar a ESP,
            # forçando o sistema a um estado limpo para evitar inconsistências.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, synchronize_and_reset_esp, esp_id)
            break

        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        
        # Tenta verificar a presença do Wi-Fi na rede.
        loop = asyncio.get_running_loop()
        is_present = await loop.run_in_executor(None, check_presence, wifi_mac)

        if is_present:
            # SUCESSO: O Wi-Fi foi encontrado.
            print(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac}' encontrado na rede.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                if bed:
                    # O evento original agora é "Resolvido".
                    _update_event_status(
                        original_event_id, 
                        status="Resolvido", 
                        detail=f"Cama '{bed.nome_cama}' associada e confirmada no quarto '{bed.quarto}' via nova tentativa."
                    )
                    # Monta o payload e o despacha para o sistema final (Connecta).
                    dispatch_payload = {
                        "quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET",
                        "dataOn": datetime.now(timezone.utc).isoformat(),
                    }
                    await loop.run_in_executor(None, dispatch_event, dispatch_payload)
                break # Encerra a tarefa de retry.
            finally:
                db.close()
        
        # Lógica de Alerta (Warning): Se o Wi-Fi continua ausente após o delay de alerta...
        elif not warning_created and (datetime.now(timezone.utc) - start_time > timedelta(minutes=warning_delay_minutes)):
            print(f"[aggregator-retry] MAC '{wifi_mac}' ausente por mais de {warning_delay_minutes} min. GERANDO WARNING.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
                
                if bed and emb:
                    timestamp_agora = datetime.now(timezone.utc)
                    # Cria um novo evento do tipo WARNING no banco de dados.
                    warning_event = ReceivedEvent(
                        esp_id=esp_id, cama=beacon_mac,
                        action="WARNING", status="OK",
                        status_detail=f"Alerta gerado para cama '{bed.nome_cama}' com MAC ausente por mais de {warning_delay_minutes} min.",
                        data_on=timestamp_agora,
                        raw={"reason": "Auto-generated by aggregator", "original_event_id": original_event_id}
                    )
                    db.add(warning_event)
                    db.commit()
                    # Monta e despacha o payload de WARNING para o sistema final.
                    warning_payload = {
                        "quarto": emb.quarto, "cama": bed.nome_cama, 
                        "status": "WARNING", "dataOn": timestamp_agora.isoformat()
                    }
                    await loop.run_in_executor(None, dispatch_event, warning_payload)
                    warning_created = True # Garante que só um warning seja gerado.
            finally:
                db.close()
    
    # Limpeza final: remove a tarefa do dicionário de pendências.
    if wifi_mac in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac]
    print(f"[aggregator-retry] Finalizada tarefa de verificação para MAC Wi-Fi '{wifi_mac}'.")


# --- Seção: Processamento de Lotes de Eventos ---
# Esta função processa um grupo de eventos que pertencem ao mesmo beacon/cama.
# É aqui que a lógica de decisão (veredito) acontece.
async def _process_events_batch(events: list):
    loop = asyncio.get_running_loop()
    db = SessionLocal()
    
    try:
        # 1. Prioridade para Eventos de Saída ('OUT'):
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
                # ================== NOVA LÓGICA AQUI ==================
                # 1. Antes de qualquer ação, verifica se existe um evento pendente para esta cama.
                pending_event = db.query(ReceivedEvent).filter(
                    ReceivedEvent.cama == beacon_mac,
                    ReceivedEvent.status == 'Pendente'
                ).first()

                if pending_event:
                    print(f"[aggregator] Encontrado evento pendente (ID: {pending_event.id}) para a cama. Cancelando...")
                    # 2. Cancela a tarefa de verificação de Wi-Fi em segundo plano.
                    cancel_pending_task(wifi_mac=bed.mac_address)
                    
                    # 3. Atualiza o status do evento pendente para "Cancelado".
                    pending_event.status = "Cancelado"
                    pending_event.status_detail = "Cancelado por evento de saída subsequente."
                    db.commit() # Salva a mudança do evento pendente.
                # ======================================================
                
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

        # 2. Processamento de Eventos de Entrada ('GET'):
        get_events = [e for e in events if e.get("status") == "GET"]
        if not get_events:
            return

        # O Veredito: O evento com o maior RSSI (sinal mais forte) é o vencedor.
        best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
        event_id = best_event.get("event_id")
        beacon_mac = best_event.get("cama")
        transacao_id = best_event.get("transacao_id")

        # Notifica a ESP vencedora via MQTT.
        print(f"[aggregator-verdict] Enviando WIN (ID: {transacao_id}) para a ESP {best_event['esp_id']}")
        publish_to_esp_channel(
            esp_id=best_event['esp_id'],
            message_type="verdict",
            data={"cama": beacon_mac, "status": "WIN", "transacao_id": transacao_id}
        )

        # Notifica as ESPs perdedoras e atualiza seus eventos no DB.
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
        
        # Se a cama já está no quarto correto, apenas confirma e encerra.
        if bed.quarto == emb.quarto:
            _update_event_status(event_id, "Confirmado", f"Cama '{bed.nome_cama}' já estava no quarto '{emb.quarto}'.")
            return
        
        # Associação: Atualiza o quarto da cama no banco de dados.
        bed.quarto = emb.quarto
        db.commit()
        
        # 3. Verificação Final de Presença do Wi-Fi:
        is_present = await loop.run_in_executor(None, check_presence, bed.mac_address)
        
        if is_present:
            # Se o Wi-Fi está online, a operação foi um sucesso.
            publish_available_beds() # Notifica que a cama não está mais livre.
            dispatch_payload = {"quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET", "dataOn": datetime.now(timezone.utc).isoformat(), "wifi": best_event.get("wifi")}
            await loop.run_in_executor(None, dispatch_event, dispatch_payload)
            _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' associada e confirmada no quarto '{emb.quarto}'.")
        else:
            # Se o Wi-Fi não está online, o status do evento vira "Pendente".
            _update_event_status(event_id, "Pendente", f"Cama associada ao quarto '{emb.quarto}', aguardando confirmação do Wi-Fi.")
            # E a tarefa de verificação em background é iniciada.
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
    """ Função pública para cancelar uma tarefa de verificação pendente externamente. """
    if wifi_mac in _pending_mac_checks:
        task = _pending_mac_checks[wifi_mac]
        task.cancel()
        del _pending_mac_checks[wifi_mac]
        print(f"[aggregator-cancel] Tarefa para o MAC {wifi_mac} foi cancelada externamente.")
        return True
    
    print(f"[aggregator-cancel] Nenhuma tarefa pendente encontrada para o MAC {wifi_mac}.")
    return False


# --- Seção: Loop Principal do Agregador ---
# Esta função roda continuamente em segundo plano, orquestrando todo o processo.
async def main_aggregator_loop():
    print(f"[aggregator] Agregador com lógica de status iniciada. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")
    last_heartbeat_time = time.time()
    heartbeat_interval_sec = 3600
    
    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)
        
        # Log periódico para saber que o processo está vivo.
        if time.time() - last_heartbeat_time > heartbeat_interval_sec:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Aggregator Heartbeat: Processo ativo.")
            last_heartbeat_time = time.time()

        if not _buffer:
            continue

        # Processamento em lote:
        events_to_process = list(_buffer)
        _buffer.clear()

        # Agrupa os eventos por beacon, pois a disputa é por cama.
        events_by_beacon = defaultdict(list)
        for evt in events_to_process:
            if "cama" in evt:
                events_by_beacon[evt["cama"]].append(evt)
        
        print(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        # Chama a função de processamento para cada grupo de eventos.
        for beacon_mac, events in events_by_beacon.items():
            await _process_events_batch(events)