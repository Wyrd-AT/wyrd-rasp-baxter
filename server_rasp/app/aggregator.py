# app/aggregator.py

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from .dispatcher import dispatch_event
from .models import SessionLocal, Bed, Embarcado
from .presence import check_presence
from .mqtt_client import publish_available_beds

# --- Configurações ---
RETRY_PRESENCE_FREQUENCY_SEC = 300  # Tentar novamente a cada 60 segundos
AGGREGATOR_LOOP_INTERVAL_SEC = 2   # O agregador processa o buffer a cada 2 segundos

# --- Estruturas de Dados em Memória ---
_buffer = []  # Fila de eventos brutos vindos da ESP
_pending_mac_checks = {}  # Dicionário para rastrear tarefas de retry {mac_wifi: asyncio.Task}

def enqueue_event(evt):
    """ Coloca um novo evento no buffer para ser processado. """
    _buffer.append(evt)
    print(f"[aggregator] Evento enfileirado: {evt}")


async def _retry_presence_task(wifi_mac: str, beacon_mac: str, original_event: dict):
    """
    Tarefa que fica em segundo plano, tentando verificar a presença de um MAC Wi-Fi.
    """
    print(f"[aggregator-retry] Iniciada tarefa para Beacon '{beacon_mac}' (verificando Wi-Fi MAC: {wifi_mac}).")
    
    while True:
        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        
        print(f"[aggregator-retry] Tentando verificar presença do MAC Wi-Fi '{wifi_mac}'...")
        if check_presence(wifi_mac):
            print(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac}' encontrado na rede.")
            db = SessionLocal()
            try:
                bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
                emb = db.query(Embarcado).filter(Embarcado.id_esp == original_event["esp_id"]).first()

                if emb and bed and bed.quarto is None:
                    print(f"[aggregator-retry] Associando cama '{bed.nome_cama}' ao quarto '{emb.quarto}' via retry.")
                    bed.quarto = emb.quarto
                    db.commit()
                    
                    # Dispara a atualização da lista de camas disponíveis
                    publish_available_beds()

                    # Monta e despacha o evento final
                    dispatch_payload = {
                        "quarto": bed.quarto,
                        "cama": bed.nome_cama,
                        "status": "IN",
                        "dataOn": datetime.now(timezone.utc).isoformat(),
                        "wifi": original_event.get("wifi")
                    }
                    dispatch_event(dispatch_payload)
                
                # Se encontrou o MAC, a tarefa termina com sucesso.
                break 

            finally:
                db.close()
    
    # Limpa a si mesma do dicionário de tarefas pendentes ao terminar
    if wifi_mac in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac]
    print(f"[aggregator-retry] Finalizada tarefa de verificação para MAC Wi-Fi '{wifi_mac}'.")


def _process_events_batch(events: list):
    """
    Processa um lote de eventos que foram agrupados por beacon.
    """
    # 1. Lógica de 'OUT': Se qualquer evento for 'OUT', cancela tudo e desassocia.
    if any(e.get("status") == "OUT" for e in events):
        beacon_mac = events[0].get("cama")
        print(f"[aggregator] Evento 'OUT' detectado para o beacon '{beacon_mac}'.")
        
        db = SessionLocal()
        try:
            bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
            if bed:
                # Cancela qualquer tarefa de retry pendente para esta cama
                if bed.mac_address in _pending_mac_checks:
                    print(f"[aggregator] Cancelando tarefa de retry pendente para MAC Wi-Fi '{bed.mac_address}'.")
                    _pending_mac_checks[bed.mac_address].cancel()
                    del _pending_mac_checks[bed.mac_address]
                
                # Se a cama estava associada a um quarto, desassocia.
                if bed.quarto is not None:
                    print(f"[aggregator] Desassociando cama '{bed.nome_cama}' do quarto '{bed.quarto}'.")
                    bed.quarto = None
                    db.commit()
                    publish_available_beds() # Atualiza a lista MQTT

        finally:
            db.close()
        return # Termina o processamento para este beacon

    # 2. Lógica de 'GET': Encontra o melhor sinal
    get_events = [e for e in events if e.get("status") == "GET"]
    if not get_events:
        return

    best_event = max(get_events, key=lambda e: e.get("RSSI", -1000))
    beacon_mac = best_event.get("cama")
    print(f"[aggregator] Melhor sinal para beacon '{beacon_mac}': RSSI {best_event['RSSI']} do ESP '{best_event['esp_id']}'.")

    # 3. Verifica a presença do MAC Wi-Fi associado
    db = SessionLocal()
    try:
        bed = db.query(Bed).filter(Bed.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == best_event['esp_id']).first()

        if not bed or not emb:
            print(f"[aggregator] ERRO: Cama (beacon: {beacon_mac}) ou ESP ({best_event['esp_id']}) não cadastrados.")
            return

        # Se a cama já está no quarto correto, não faz nada.
        if bed.quarto == emb.quarto:
            print(f"[aggregator] Confirmação: Cama '{bed.nome_cama}' já está corretamente no quarto '{emb.quarto}'.")
            return
        
        # Se a cama está vaga, tenta associá-la
        if bed.quarto is None:
            if check_presence(bed.mac_address):
                print(f"[aggregator] Presença da cama '{bed.nome_cama}' (MAC: {bed.mac_address}) confirmada. Associando ao quarto '{emb.quarto}'.")
                bed.quarto = emb.quarto
                db.commit()
                publish_available_beds()
                
                dispatch_payload = {"quarto": bed.quarto, "cama": bed.nome_cama, "status": "IN", "dataOn": datetime.now(timezone.utc).isoformat(), "wifi": best_event.get("wifi")}
                dispatch_event(dispatch_payload)
            else:
                # Se a presença falhar, inicia a tarefa de retry (se já não houver uma)
                print(f"[aggregator] Presença da cama '{bed.nome_cama}' (MAC: {bed.mac_address}) FALHOU.")
                if bed.mac_address not in _pending_mac_checks:
                    task = asyncio.create_task(_retry_presence_task(bed.mac_address, beacon_mac, best_event))
                    _pending_mac_checks[bed.mac_address] = task
                else:
                    print(f"[aggregator] Tarefa de retry para MAC Wi-Fi '{bed.mac_address}' já está em andamento.")
    finally:
        db.close()


async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print(f"[aggregator] Agregador com lógica de retry iniciado. Loop a cada {AGGREGATOR_LOOP_INTERVAL_SEC}s.")
    
    while True:
        await asyncio.sleep(AGGREGATOR_LOOP_INTERVAL_SEC)
        
        if not _buffer:
            continue

        # Copia o buffer e o limpa para não processar os mesmos eventos duas vezes
        events_to_process = list(_buffer)
        _buffer.clear()

        # Agrupa os eventos por beacon para processar em lotes
        events_by_beacon = defaultdict(list)
        for evt in events_to_process:
            events_by_beacon[evt["cama"]].append(evt)
        
        print(f"\n[aggregator] Processando {len(events_to_process)} eventos para {len(events_by_beacon)} beacons...")
        for beacon_mac, events in events_by_beacon.items():
            _process_events_batch(events)