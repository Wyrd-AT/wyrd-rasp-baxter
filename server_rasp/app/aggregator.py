# ==============================================================================
# ARQUIVO: aggregator.py
# ==============================================================================
"""
Propósito do Arquivo:
O cérebro do sistema: processa eventos, resolve disputas e gerencia o estado.

Funções Chave no Fluxo:
- `main_aggregator_loop()`: Roda para sempre. Pega eventos da fila, agrupa
  por cama e os entrega para `_process_events_batch`.
- `_process_events_batch(events)`:
  1. Se recebe um evento 'OUT', desassocia a cama e cancela tarefas pendentes.
  2. Se recebe eventos 'GET', elege o melhor RSSI como "WIN" e os outros
     como "LOSE", notificando os ESPs via MQTT.
  3. Tenta associar a cama ao quarto do ESP vencedor.
  4. Chama `check_presence()` para validar o Wi-Fi da cama.
  5. Se o Wi-Fi está OK, despacha o evento final. Se não, inicia
     `_retry_presence_task`.
- `_retry_presence_task(...)`: Tarefa em segundo plano que fica tentando
  validar um Wi-Fi ausente. Se conseguir, finaliza a associação; se falhar
  após um tempo, reverte a operação e reseta a ESP.
"""

import asyncio
import time 
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional 

# Importa os módulos necessários para interagir com o resto do sistema.
from .dispatcher import dispatch_event
from .models import SessionLocal, Bed, Embarcado, ReceivedEvent
from .config import settings
from .presence import check_presence
from .mqtt_client import publish_available_beds, publish_to_esp_channel
from .services import synchronize_and_reset_esp, update_bed_assignment

import logging
logger = logging.getLogger(__name__)

# --- Seção: Configurações e Estruturas de Dados em Memória ---
RETRY_PRESENCE_FREQUENCY_SEC = 20 # A cada quantos segundos tentar verificar a presença de um Wi-Fi ausente.
AGGREGATOR_LOOP_INTERVAL_SEC = 2  # Frequência do loop principal do agregador.

_buffer = [] # Fila para novos eventos.
_pending_mac_checks = {} # Dicionário de tarefas de verificação de presença em andamento.

def get_pending_macs():
    """Retorna uma lista dos MACs de Wi-Fi que estão em verificação pendente."""
    return list(_pending_mac_checks.keys())

def enqueue_event(evt: dict):
    """ Coloca um novo evento no buffer para ser processado pelo loop principal. """
    _buffer.append(evt)
    logger.info(f"[aggregator] Evento enfileirado: {evt}")

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
        logger.error(f"[aggregator-update] ERRO ao atualizar evento {event_id}: {e}")
        db.rollback()
    finally:
        db.close()

# --- Seção: Tarefa de Verificação de Presença (Retry Task) ---
# Esta tarefa assíncrona é disparada quando uma cama é detectada por um ESP,
# mas o seu módulo Wi-Fi ainda não está visível na rede. Ela fica tentando
# encontrar o Wi-Fi em intervalos regulares.
async def retry_presence_task(wifi_mac: str, beacon_mac: str, original_event_id: int, esp_id: str, wifi_signal: Optional[int]):
    logger.info(f"[aggregator-retry] Iniciada tarefa para Beacon '{beacon_mac}' (MAC Wi-Fi: {wifi_mac}).")
    
    start_time = datetime.now(timezone.utc)
    warning_created = False
    warning_delay_minutes = int(settings.get('warning_delay_minutes', 5))
    max_pending_minutes = int(settings.get("max_pending_minutes", 15))

    while True:
        # Lógica de Timeout (Vencido): Se a verificação demorar mais que o tempo máximo...
        if datetime.now(timezone.utc) - start_time > timedelta(minutes=max_pending_minutes):
            logger.info(f"[aggregator-retry] MAC '{wifi_mac}' pendente por mais de {max_pending_minutes} min. Marcando como Vencido.")
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
        is_present = await check_presence(wifi_mac)

        if is_present:
            # SUCESSO: O Wi-Fi foi encontrado.
            logger.info(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac}' encontrado na rede.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                if bed:
                    # O evento original agora é "Resolvido".
                    logger.info("[aggregator-retry] Wi-Fi confirmado. Aguardando 5s para estabilização do serviço...")
                    delay_wifi_to_server = int(settings.get("delay_wifi_to_server", 5))
                    await asyncio.sleep(delay_wifi_to_server)

                    br_timezone = timezone(timedelta(hours=-3))
                    timestamp_agora_br = datetime.now(br_timezone)

                    dispatch_payload = {
                        "quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET",
                        "dataOn": timestamp_agora_br.isoformat(), 
                        "wifi": wifi_signal
                    }

                    success = await loop.run_in_executor(None, dispatch_event, dispatch_payload)

                    if success:
                        # Se o envio foi bem-sucedido, marca como "Resolvido"
                        _update_event_status(
                            original_event_id, 
                            status="Resolvido", 
                            detail=f"Cama '{bed.nome_cama}' associada, confirmada e enviada com sucesso."
                        )
                    else:
                        # Se falhou, marca como "Erro"
                        _update_event_status(
                            original_event_id,
                            status="Erro",
                            detail="A presença do Wi-Fi foi confirmada, mas a comunicação com o servidor final falhou."
                        )
                    break
            finally:
                db.close()
        
        elif not warning_created and (datetime.now(timezone.utc) - start_time > timedelta(minutes=warning_delay_minutes)):
            logger.info(f"[aggregator-retry] MAC '{wifi_mac}' ausente por mais de {warning_delay_minutes} min. GERANDO WARNING.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
                
                if bed and emb:
                    # --- CORREÇÃO AQUI ---
                    # 1. Define o nosso fuso horário (UTC-3)
                    br_timezone = timezone(timedelta(hours=-3))
                    # 2. Pega a hora atual NESTE fuso horário
                    timestamp_agora = datetime.now(br_timezone)
                    # --- FIM DA CORREÇÃO ---

                    # Cria um novo evento do tipo WARNING no banco de dados.
                    warning_event = ReceivedEvent(
                        esp_id=esp_id, cama=beacon_mac,
                        action="WARNING", status="OK",
                        status_detail=f"Alerta gerado para cama '{bed.nome_cama}' com MAC ausente por mais de {warning_delay_minutes} min.",
                        data_on=timestamp_agora, # Agora com o horário correto
                        raw={"reason": "Auto-generated by aggregator", "original_event_id": original_event_id}
                    )
                    db.add(warning_event)
                    db.commit()
                    # Monta e despacha o payload de WARNING para o sistema final.
                    warning_payload = {
                        "quarto": emb.quarto, "cama": bed.nome_cama, 
                        "status": "WARNING", "dataOn": timestamp_agora.isoformat() # Agora com o horário correto
                    }
                    await loop.run_in_executor(None, dispatch_event, warning_payload)
                    warning_created = True # Garante que só um warning seja gerado.
            finally:
                db.close()
    
    # Limpeza final: remove a tarefa do dicionário de pendências.
    if wifi_mac in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac]
    logger.info(f"[aggregator-retry] Finalizada tarefa de verificação para MAC Wi-Fi '{wifi_mac}'.")


# --- Seção: Processamento de Lotes de Eventos ---
# Esta função processa um grupo de eventos que pertencem ao mesmo beacon/cama.
# É aqui que a lógica de decisão (veredito) acontece.
async def _process_events_batch(events: list):
    loop = asyncio.get_running_loop()
    db = SessionLocal()
    
    try:
        # --- 1. PROCESSAMENTO DE EVENTOS 'OUT' (Sem alterações) ---
        out_events = [e for e in events if e.get("status") == "OUT"]
        if out_events:
            # (Toda a sua lógica de 'OUT', que já está correta, continua aqui)
            main_out_event = out_events[0]
            event_id = main_out_event.get("event_id")
            beacon_mac = main_out_event.get("cama")
            logger.info(f"[aggregator] Evento 'OUT' detectado para o beacon '{beacon_mac}'.")
            
            for evt in out_events[1:]:
                _update_event_status(evt.get("event_id"), "Ignorado", "Evento OUT duplicado no mesmo lote.")

            bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
            if bed:
                task_cancelled = cancel_pending_task(wifi_mac=bed.mac_address)
                if task_cancelled:
                    pending_event = db.query(ReceivedEvent).filter(
                        ReceivedEvent.cama == beacon_mac,
                        ReceivedEvent.status == 'Pendente'
                    ).order_by(ReceivedEvent.data_on.desc()).first()

                    if pending_event:
                        logger.info(f"[aggregator] Encontrado e cancelado evento pendente (ID: {pending_event.id}). Atualizando status.")
                        pending_event.status = "Cancelado"
                        pending_event.status_detail = "Cancelado por evento de saída subsequente."
                
                quarto_anterior = bed.quarto
                update_bed_assignment(bed_id=bed.id, new_room=None)
                
                if quarto_anterior is not None:
                    _update_event_status(event_id, "OK", f"Cama '{bed.nome_cama}' desassociada com sucesso.")
                else:
                    _update_event_status(event_id, "Confirmado", "Cama já estava desassociada.")
                db.commit()
            else:
                _update_event_status(event_id, "Erro", f"Cama com beacon '{beacon_mac}' não cadastrada.")
            return

        # --- 2. PROCESSAMENTO DE EVENTOS 'GET' (Lógica Reestruturada) ---
        get_events = [e for e in events if e.get("status") == "GET"]
        if not get_events:
            return

        # ETAPA A: Achar o vencedor baseado no RSSI (isso não muda)
        best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
        event_id = best_event.get("event_id")
        beacon_mac = best_event.get("cama")
        
        bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == best_event['esp_id']).first()

        if not bed or not emb:
            _update_event_status(event_id, "Erro", "Componente (cama ou ESP) não cadastrado.")
            return
        
        logger.info(f"[aggregator] Novo GET recebido para '{beacon_mac}'. Cancelando tarefas pendentes anteriores...")
        cancel_pending_task(wifi_mac=bed.mac_address)
        
        # ETAPA B: Validar o estado da cama ANTES de enviar qualquer veredito
        if bed.quarto is not None:
            # Cenário 1: A cama já está no quarto correto. É apenas uma confirmação de sinal.
            if bed.quarto == emb.quarto:
                _update_event_status(event_id, "Confirmado", f"Cama '{bed.nome_cama}' já estava no quarto '{emb.quarto}'.")
                # Enviamos um WIN de confirmação para que a ESP não fique presa esperando um veredito.
                publish_to_esp_channel(
                    esp_id=best_event['esp_id'], message_type="verdict",
                    data={"cama": beacon_mac, "status": "WIN", "transacao_id": best_event.get("transacao_id")}
                )
                return

            # Cenário 2: A cama está associada a OUTRO quarto.
            else:
                # Cenário 2a: A associação ao outro quarto ainda está PENDENTE.
                if bed.mac_address in _pending_mac_checks:
                    logger.info(f"[aggregator] BLOQUEIO (PENDENTE): Cama {beacon_mac} já está em processo pendente. Ignorando GET da ESP {best_event['esp_id']}.")
                    _update_event_status(event_id, "Ignorado", "A cama já está em um processo de associação pendente.")
                    publish_to_esp_channel(
                        esp_id=best_event['esp_id'], message_type="verdict",
                        data={"cama": beacon_mac, "status": "LOSE", "transacao_id": best_event.get("transacao_id")}
                    )
                    publish_available_beds()
                    return

                # Cenário 2b: A associação ao outro quarto já está CONFIRMADA. (ESTA É A CORREÇÃO PRINCIPAL)
                else:
                    logger.info(f"[aggregator] BLOQUEIO (CONFIRMADO): Tentativa da ESP {best_event['esp_id']} de 'roubar' a cama '{beacon_mac}', que já pertence ao quarto '{bed.quarto}'.")
                    _update_event_status(event_id, "Ignorado", f"A cama já está confirmada no quarto {bed.quarto}.")
                    publish_to_esp_channel(
                        esp_id=best_event['esp_id'], message_type="verdict",
                        data={"cama": beacon_mac, "status": "LOSE", "transacao_id": best_event.get("transacao_id")}
                    )
                    publish_available_beds()
                    return

        # ETAPA C: Enviar os Vereditos (APÓS a validação)
        # Se chegamos aqui, a associação é legítima. Agora sim podemos enviar os vereditos.
        transacao_id = best_event.get("transacao_id")
        logger.info(f"[aggregator-verdict] Enviando WIN (ID: {transacao_id}) para a ESP {best_event['esp_id']}")
        publish_to_esp_channel(
            esp_id=best_event['esp_id'], message_type="verdict",
            data={"cama": beacon_mac, "status": "WIN", "transacao_id": transacao_id}
        )
        for evt in get_events:
            if evt is not best_event:
                _update_event_status(evt.get("event_id"), "Ignorado", "Sinal mais fraco. Veredito: LOSE.")
                publish_to_esp_channel(
                    esp_id=evt['esp_id'], message_type="verdict",
                    data={"cama": beacon_mac, "status": "LOSE", "transacao_id": transacao_id}
                )
        
        # ETAPA D: Associar a cama e verificar o Wi-Fi (como antes)
        bed.quarto = emb.quarto
        db.commit()
        
        is_present = await check_presence(bed.mac_address)
        publish_available_beds()
        
        if is_present:
            logger.info("[aggregator] Wi-Fi confirmado. Aguardando 5s para estabilização do serviço na cama...")
            await asyncio.sleep(5)
            br_timezone = timezone(timedelta(hours=-3))
            timestamp_agora_br = datetime.now(br_timezone)
            dispatch_payload = {"quarto": bed.quarto, "cama": bed.nome_cama, "status": "GET", "dataOn": timestamp_agora_br.isoformat(), "wifi": best_event.get("wifi")}
            success = await loop.run_in_executor(None, dispatch_event, dispatch_payload)

            if success:
                # Se o envio foi bem-sucedido, marca como "OK"
                _update_event_status(
                    event_id, "OK", 
                    f"Cama '{bed.nome_cama}' associada, confirmada e enviada com sucesso."
                )
            else:
                # Se falhou, marca como "Erro"
                _update_event_status(
                    event_id, "Erro",
                    "A cama foi associada e o Wi-Fi confirmado, mas a comunicação com o servidor final falhou."
                )
        else:
            _update_event_status(event_id, "Pendente", f"Cama associada ao quarto '{emb.quarto}', aguardando confirmação do Wi-Fi.")
            if bed.mac_address not in _pending_mac_checks:
                wifi_signal = best_event.get("wifi")
                task = asyncio.create_task(
                    retry_presence_task(
                        wifi_mac=bed.mac_address, 
                        beacon_mac=beacon_mac, 
                        original_event_id=event_id,
                        esp_id=best_event['esp_id'],
                        wifi_signal=wifi_signal 
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
        logger.info(f"[aggregator-cancel] Tarefa para o MAC {wifi_mac} foi cancelada externamente.")
        return True
    
    logger.info(f"[aggregator-cancel] Nenhuma tarefa pendente encontrada para o MAC {wifi_mac}.")
    return False


# --- Seção: Loop Principal do Agregador ---
# Esta função roda continuamente em segundo plano, orquestrando todo o processo.
async def main_aggregator_loop():
    logger.info(f"[aggregator] Agregador com lógica de status iniciada. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")
    last_heartbeat_time = time.time()
    heartbeat_interval_sec = 3600
    
    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)
        
        # Log periódico para saber que o processo está vivo.
        if time.time() - last_heartbeat_time > heartbeat_interval_sec:
            logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Aggregator Heartbeat: Processo ativo.")
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
        
        logger.info(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        # Chama a função de processamento para cada grupo de eventos.
        for beacon_mac, events in events_by_beacon.items():
            asyncio.create_task(_process_events_batch(events))