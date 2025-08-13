# aggregator.py (Versão RTLS Final - Responsabilidade Única)
import asyncio
import time
from .models import SessionLocal, Asset, Embarcado
from .services import update_asset_assignment # A única interface com o DB
from .mqtt_client import scan_data_queue
import logging

logger = logging.getLogger(__name__)

_asset_realtime_state = {}
PROCESS_INTERVAL_SEC = 2.0
READING_TIMEOUT_SEC = 10

# ... (a função _consume_scan_data_queue permanece exatamente a mesma) ...
async def _consume_scan_data_queue():
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id = item.get("esp_id")
        payload = item.get("payload", {})
        beacons = payload.get("beacons", [])
        timestamp = payload.get("timestamp", time.time())
        for beacon in beacons:
            mac = beacon.get("mac", "").lower()
            rssi = beacon.get("rssi")
            if not mac or not rssi: continue
            if mac not in _asset_realtime_state:
                _asset_realtime_state[mac] = {}
            _asset_realtime_state[mac][esp_id] = {"rssi": rssi, "timestamp": timestamp}


async def _processar_localizacoes():
    db = SessionLocal()
    try:
        esp_map = {e.id_esp: e.quarto_id for e in db.query(Embarcado).all()}
        asset_map = {a.mac_beacon: (a.id, a.quarto_id) for a in db.query(Asset).all()}
        now = time.time()

        for mac, readings in list(_asset_realtime_state.items()):
            recent_readings = {
                esp_id: data for esp_id, data in readings.items()
                if now - data["timestamp"] < READING_TIMEOUT_SEC
            }
            
            if not recent_readings:
                del _asset_realtime_state[mac]
                if mac in asset_map and asset_map[mac][1] is not None:
                    asset_id, _ = asset_map[mac]
                    # --- CHAMADA AO SERVIÇO SIMPLIFICADA ---
                    await update_asset_assignment(db, asset_id, None, "server", details="Ativo desapareceu da rede.")
                continue

            best_esp_id = max(recent_readings, key=lambda esp: recent_readings[esp]["rssi"])
            best_rssi = recent_readings[best_esp_id]["rssi"]
            
            if best_esp_id not in esp_map or mac not in asset_map: continue

            novo_quarto_id = esp_map[best_esp_id]
            asset_id, quarto_id_atual = asset_map[mac]

            if novo_quarto_id != quarto_id_atual:
                # --- CHAMADA AO SERVIÇO SIMPLIFICADA ---
                details = f"Localizado via ESP {best_esp_id} com RSSI {best_rssi}."
                await update_asset_assignment(db, asset_id, novo_quarto_id, best_esp_id, best_rssi, details)
            
            _asset_realtime_state[mac] = recent_readings
    finally:
        db.close()

async def main_aggregator_loop():
    logger.info(f"[RTLS] Motor de localização iniciado.")
    while True:
        try:
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico no loop do agregador: {e}", exc_info=True)
        await asyncio.sleep(PROCESS_INTERVAL_SEC)