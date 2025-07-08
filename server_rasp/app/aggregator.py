# aggregator.py (versão com gatilhos MQTT manuais e corretos)

import asyncio
from collections import defaultdict
from .presence import check_presence
from .dispatcher import dispatch_event
from .models import SessionLocal, Embarcado, Bed
from .mqtt_client import publish_available_beds # <--- IMPORTA A FUNÇÃO

# --- Configurações e Estruturas de Dados (sem alteração) ---
RETRY_PRESENCE_FREQUENCY_SEC = 60
_buffer = []
_beds_in_process = set()
_pending_mac_checks = {}

def enqueue_event(evt):
    _buffer.append(evt)
    print(f"[aggregator] enqueue: {evt}")

async def retry_mac_check(wifi_mac_address: str, event_data: dict):
    mac_beacon = event_data.get("cama")
    print(f"[aggregator-retry] Iniciada tarefa para Beacon '{mac_beacon}' (verificando Wi-Fi MAC: {wifi_mac_address}).")
    
    while True:
        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        db = SessionLocal()
        try:
            if check_presence(wifi_mac_address):
                print(f"[aggregator-retry] SUCESSO! MAC Wi-Fi '{wifi_mac_address}' encontrado.")
                
                bed = db.query(Bed).filter(Bed.mac_beacon == mac_beacon).first()
                emb = db.query(Embarcado).filter(Embarcado.id_esp == event_data["esp_id"]).first()
                
                if emb and bed and bed.quarto is None:
                    print(f"[aggregator-retry] Associando cama '{bed.nome_cama}' via retry ao quarto '{emb.quarto}'.")
                    bed.quarto = emb.quarto
                    db.commit()
                    publish_available_beds() # <--- GATILHO MQTT ADICIONADO AQUI

                    dispatch_payload = event_data.copy()
                    dispatch_payload.update({"quarto": bed.quarto, "status": "GET", "mac_address": bed.mac_address, "cama": bed.nome_cama})
                    dispatch_event(dispatch_payload)
                
                break
        finally:
            db.close()

    if wifi_mac_address in _pending_mac_checks:
        del _pending_mac_checks[wifi_mac_address]
    print(f"[aggregator-retry] Finalizada tarefa para Beacon '{mac_beacon}'.")


async def process_bed_events(mac_beacon: str):
    _beds_in_process.add(mac_beacon)
    db = SessionLocal()
    try:
        events_for_bed = [e for e in _buffer if e.get("cama") == mac_beacon]
        if not events_for_bed: return

        bed = db.query(Bed).filter(Bed.mac_beacon == mac_beacon).first()
        if not bed:
            for ev in list(_buffer):
                if ev.get("cama") == mac_beacon: _buffer.remove(ev)
            return

        wifi_mac_address = bed.mac_address

        if any(e.get("status") == "OUT" for e in events_for_bed):
            if wifi_mac_address in _pending_mac_checks:
                _pending_mac_checks[wifi_mac_address].cancel()
                del _pending_mac_checks[wifi_mac_address]
            
            if bed.quarto is not None:
                print(f"[aggregator] Recebido 'OUT' para beacon '{mac_beacon}' (Cama: {bed.nome_cama}). Removendo do quarto '{bed.quarto}'.")
                bed.quarto = None
                db.commit()
                publish_available_beds() # <--- GATILHO MQTT ADICIONADO AQUI
            
            for ev in list(_buffer):
                if ev.get("cama") == mac_beacon: _buffer.remove(ev)
            return

        best_event = max(events_for_bed, key=lambda e: e.get("RSSI", -1000))
        print(f"[aggregator] FILTRO PARA BEACON '{mac_beacon}': {len(events_for_bed)} eventos. Vencedor: ESP '{best_event['esp_id']}' com RSSI {best_event['RSSI']}.")
        
        esp_id = best_event["esp_id"]
        emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()

        if not emb:
            for ev in list(_buffer):
                if ev.get("cama") == mac_beacon: _buffer.remove(ev)
            return
        
        if check_presence(wifi_mac_address):
            if wifi_mac_address in _pending_mac_checks:
                _pending_mac_checks[wifi_mac_address].cancel()
                del _pending_mac_checks[wifi_mac_address]

            if bed.quarto is None:
                print(f"[aggregator] Associando cama '{bed.nome_cama}' ao quarto '{emb.quarto}'.")
                bed.quarto = emb.quarto
                db.commit()
                publish_available_beds() # <--- GATILHO MQTT ADICIONADO AQUI
            
            elif bed.quarto != emb.quarto:
                print(f"[aggregator] Conflito Ignorado: Cama '{bed.nome_cama}' já está em '{bed.quarto}', mas foi detectada em '{emb.quarto}'.")
            
            for ev in list(_buffer):
                if ev.get("cama") == mac_beacon: _buffer.remove(ev)
        else:
            if wifi_mac_address not in _pending_mac_checks:
                task = asyncio.create_task(retry_mac_check(wifi_mac_address, best_event))
                _pending_mac_checks[wifi_mac_address] = task
            
            for ev in list(_buffer):
                if ev.get("cama") == mac_beacon: _buffer.remove(ev)
            
    finally:
        db.close()
        if mac_beacon in _beds_in_process:
            _beds_in_process.remove(mac_beacon)


async def main_aggregator_loop():
    print("[aggregator] Agregador Orientado a Eventos iniciado.")
    while True:
        await asyncio.sleep(1)
        
        pending_events_by_beacon = defaultdict(list)
        for evt in _buffer:
            if "cama" in evt:
                pending_events_by_beacon[evt["cama"]].append(evt)
            
        for mac_beacon, events in pending_events_by_beacon.items():
            if mac_beacon not in _beds_in_process:
                asyncio.create_task(process_bed_events(mac_beacon))