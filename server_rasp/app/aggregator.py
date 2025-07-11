# aggregator.py

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

# Importa o novo dispatcher e as funções/modelos renomeados/relevantes
from .dispatcher import dispatch_event_to_eritel
from .models import SessionLocal, Badge, Embarcado, ReceivedEvent
from .mqtt_client import publish_available_badges # Supondo que você renomeou a função em mqtt_client.py

# --- Configurações ---
AGGREGATOR_LOOP_INTERVAL_SEC = 2 # O loop pode ser rápido, já que a lógica é mais simples

# --- Estrutura de Dados em Memória ---
_buffer = []

def enqueue_event(evt: dict):
    """ Coloca um novo evento da ESP no buffer para ser processado. """
    _buffer.append(evt)
    print(f"[aggregator] Evento enfileirado: {evt}")

def _update_event_status(event_id: int, status: str, detail: str):
    """ Helper para atualizar o status de um evento no banco de dados. """
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

async def _process_events_batch(events: list):
    """
    Processa um lote de eventos para um mesmo beacon, com a lógica simplificada.
    """
    db = SessionLocal()
    try:
        # --- Lógica de 'OUT' (Saída) ---
        out_events = [e for e in events if e.get("status") == "OUT"]
        if out_events:
            main_out_event = out_events[0]
            event_id = main_out_event.get("event_id")
            beacon_mac = main_out_event.get("cama") # 'cama' ainda é a chave vinda da ESP
            print(f"[aggregator] Evento 'OUT' detectado para o beacon '{beacon_mac}'.")

            # Marca eventos 'OUT' duplicados como ignorados
            for evt in out_events[1:]:
                _update_event_status(evt.get("event_id"), "Ignorado", "Evento OUT duplicado no mesmo lote.")

            badge = db.query(Badge).filter(Badge.mac_beacon == beacon_mac).first()
            if badge and badge.quarto is not None:
                quarto_anterior = badge.quarto
                nome_cracha = badge.nome_cracha

                # Desassocia o crachá
                badge.quarto = None
                db.commit()
                publish_available_badges() # Notifica o MQTT sobre o crachá disponível

                # Prepara e despacha o evento para a Eritel
                event_data = {
                    "cracha": nome_cracha,
                    "quarto": quarto_anterior,
                    "data_evento": datetime.now(timezone.utc).isoformat()
                }
                dispatch_event_to_eritel("wyrd.SAIDA", event_data)

                _update_event_status(event_id, "OK", f"Crachá '{nome_cracha}' desassociado do quarto '{quarto_anterior}'.")
            elif badge:
                _update_event_status(event_id, "Confirmado", "Crachá já estava desassociado.")
            else:
                 _update_event_status(event_id, "Erro", f"Crachá com beacon '{beacon_mac}' não cadastrado.")
            return # Processa 'OUT' e encerra para este lote

        # --- Lógica de 'GET' (Entrada) ---
        get_events = [e for e in events if e.get("status") == "GET"]
        if not get_events:
            return

        # Elege o melhor evento baseado no RSSI mais forte
        best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
        event_id = best_event.get("event_id")
        beacon_mac = best_event.get("cama")
        esp_id = best_event.get("esp_id")

        # Marca os outros eventos 'GET' como ignorados
        for evt in get_events:
            if evt is not best_event:
                _update_event_status(evt.get("event_id"), "Ignorado", f"Sinal mais fraco (RSSI: {evt.get('RSSI', 'N/A')}).")

        badge = db.query(Badge).filter(Badge.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()

        if not badge or not emb:
            detail = f"Crachá (beacon: {beacon_mac})" if not badge else f"ESP ({esp_id})"
            _update_event_status(event_id, "Erro", f"Componente não cadastrado: {detail}.")
            return

        if badge.quarto == emb.quarto:
            _update_event_status(event_id, "Confirmado", f"Crachá '{badge.nome_cracha}' já estava no quarto '{emb.quarto}'.")
            return

        # Ação direta: Associa o crachá ao novo quarto
        badge.quarto = emb.quarto
        db.commit()
        publish_available_badges()

        # Prepara e despacha o evento para a Eritel
        event_data = {
            "cracha": badge.nome_cracha,
            "quarto": emb.quarto,
            "andar": emb.andar,
            "esp_id": emb.id_esp,
            "rssi": best_event.get("RSSI"),
            "data_evento": datetime.now(timezone.utc).isoformat()
        }
        dispatch_event_to_eritel("wyrd.ENTRADA", event_data)

        _update_event_status(event_id, "OK", f"Crachá '{badge.nome_cracha}' associado ao quarto '{emb.quarto}'.")

    finally:
        db.close()

async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print(f"[aggregator] Agregador SIMPLIFICADO iniciado. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")

    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)

        if not _buffer:
            continue

        events_to_process = list(_buffer)
        _buffer.clear()

        # Agrupa eventos por MAC de beacon (ainda vindo como 'cama' da ESP)
        events_by_beacon = defaultdict(list)
        for evt in events_to_process:
            if "cama" in evt:
                events_by_beacon[evt["cama"]].append(evt)

        print(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        for beacon_mac, events in events_by_beacon.items():
            # A função de processamento agora é assíncrona
            await _process_events_batch(events)

# Não há mais tarefas em background para cancelar, então a função pode ser removida ou deixada vazia.
def cancel_pending_task(wifi_mac: str) -> bool:
    """ Esta função não é mais necessária na nova lógica. """
    print(f"[aggregator-cancel] A função de cancelamento não é mais aplicável.")
    return False