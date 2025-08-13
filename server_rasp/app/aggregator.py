# aggregator.py (Versão RTLS Final e Inteligente)
import asyncio
import time
from datetime import datetime, timezone
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting
from .services import update_asset_assignment
from .mqtt_client import scan_data_queue
import logging

logger = logging.getLogger(__name__)

# --- O Novo "Objeto de Estado" em Memória ---
# Agora guarda não só as leituras, mas o estado da decisão
# Formato: { "mac_do_ativo": AssetState() }
_asset_realtime_state = {}

# --- Configurações do Motor (com valores padrão) ---
_config = {
    "process_interval_sec": 2.0,
    "reading_timeout_sec": 10,
    "default_rssi_threshold": -75,
    "conflict_margin_db": 5,
    "inertia_ms": 5000
}

class AssetState:
    """Uma classe para guardar o estado completo de um ativo em tempo real."""
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}  # { esp_id: {"rssi": ..., "timestamp": ...} }
        
        # Estado de decisão
        self.candidate_quarto_id = None
        self.candidate_since = None # Timestamp de quando se tornou candidato
        self.inertia_triggered = False

    def update_reading(self, esp_id, rssi, timestamp):
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp}

    def cleanup_old_readings(self):
        """Remove leituras com mais de X segundos."""
        now = time.time()
        self.readings = {
            esp_id: data for esp_id, data in self.readings.items()
            if now - data["timestamp"] < _config["reading_timeout_sec"]
        }
        return bool(self.readings) # Retorna True se ainda há leituras

# ==============================================================================
# O CÉREBRO DO RTLS
# ==============================================================================
async def _processar_localizacoes():
    db = SessionLocal()
    try:
        # 1. Carrega mapas de configuração para evitar queries repetidas
        esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in db.query(Embarcado).all()}
        asset_map = {a.mac_beacon: (a.id, a.quarto_id) for a in db.query(Asset).all()}
        
        # 2. Itera sobre cada ativo que o sistema está a monitorizar
        for mac, state in list(_asset_realtime_state.items()):
            
            # 3. Limpeza: Remove leituras antigas. Se não sobrar nenhuma, o ativo desapareceu.
            if not state.cleanup_old_readings():
                del _asset_realtime_state[mac]
                if mac in asset_map and asset_map[mac][1] is not None:
                    asset_id, _ = asset_map[mac]
                    await update_asset_assignment(db, asset_id, None, "server", details="Ativo desapareceu da rede.")
                continue

            # 4. Análise de Candidatos (Regra 1: Limiar de Admissão)
            strongest_signal = {"esp_id": None, "rssi": -1000, "quarto_id": None}
            
            for esp_id, reading in state.readings.items():
                if esp_id not in esp_map: continue
                
                quarto_id, custom_rssi = esp_map[esp_id]
                threshold = custom_rssi if custom_rssi is not None else _config["default_rssi_threshold"]
                
                # O sinal só é válido se estiver ACIMA do limiar daquela ESP
                if reading["rssi"] > threshold and reading["rssi"] > strongest_signal["rssi"]:
                    strongest_signal = {"esp_id": esp_id, "rssi": reading["rssi"], "quarto_id": quarto_id}

            candidate_quarto_id = strongest_signal["quarto_id"]
            asset_id, quarto_id_atual = asset_map.get(mac, (None, None))
            if not asset_id: continue

            # 5. Lógica de Inércia e Conflito
            
            # Se não há candidato forte, reseta a inércia
            if not candidate_quarto_id:
                state.candidate_quarto_id = None
                state.candidate_since = None
                state.inertia_triggered = False
                continue

            # Inicia ou mantém a inércia se o candidato mudou
            if candidate_quarto_id != state.candidate_quarto_id:
                
                # Regra 2: Margem de Conflito
                # Se já está num quarto, o novo candidato precisa de ser significativamente melhor
                if quarto_id_atual is not None and quarto_id_atual != candidate_quarto_id:
                    current_esp_reading = next((r for e, r in state.readings.items() if esp_map.get(e, (None,None))[0] == quarto_id_atual), None)
                    if current_esp_reading and strongest_signal["rssi"] < (current_esp_reading["rssi"] + _config["conflict_margin_db"]):
                        continue # O novo sinal não é forte o suficiente para justificar uma mudança, ignora

                state.candidate_quarto_id = candidate_quarto_id
                state.candidate_since = time.time()
                state.inertia_triggered = False
                logger.info(f"[RTLS-IA] Ativo {mac} é agora um candidato para o quarto {candidate_quarto_id} (via {strongest_signal['esp_id']}).")

            # Regra 3: Confirmação da Inércia
            # Se o candidato é o mesmo por tempo suficiente, e a mudança ainda não foi feita...
            if not state.inertia_triggered and state.candidate_since and (time.time() - state.candidate_since) * 1000 > _config["inertia_ms"]:
                
                if state.candidate_quarto_id != quarto_id_atual:
                    logger.info(f"[RTLS-IA] INÉRCIA CONFIRMADA para {mac}. Movendo para o quarto {state.candidate_quarto_id}.")
                    state.inertia_triggered = True # Marca que já disparámos a ação
                    
                    details = f"Localizado via {strongest_signal['esp_id']} com RSSI {strongest_signal['rssi']}"
                    await update_asset_assignment(db, asset_id, state.candidate_quarto_id, strongest_signal['esp_id'], strongest_signal['rssi'], details)
    finally:
        db.close()

# ==============================================================================
# FUNÇÕES DE SUPORTE
# ==============================================================================
async def _consume_scan_data_queue():
    """Consome a fila do MQTT e atualiza os objetos de estado."""
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id = item.get("esp_id")
        payload = item.get("payload", {})
        beacons = payload.get("beacons", [])
        timestamp = payload.get("timestamp", time.time())

        for beacon in beacons:
            mac = beacon.get("mac", "").lower()
            if not mac: continue

            if mac not in _asset_realtime_state:
                _asset_realtime_state[mac] = AssetState(mac)
            
            _asset_realtime_state[mac].update_reading(esp_id, beacon.get("rssi"), timestamp)

def _load_config_from_db():
    """Carrega as configurações do motor a partir da base de dados."""
    db = SessionLocal()
    try:
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        _config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        _config["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", _config["conflict_margin_db"]))
        _config["inertia_ms"] = int(settings_from_db.get("inercia_mudanca", _config["inertia_ms"]))
        logger.info(f"[RTLS-IA] Configurações do motor carregadas: {str(_config)}")
    finally:
        db.close()

async def main_aggregator_loop():
    """ O "ciclo de relógio" do motor RTLS. """
    _load_config_from_db()
    logger.info(f"[RTLS] Motor de localização INTELIGENTE iniciado.")
    while True:
        try:
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico no loop principal: {e}", exc_info=True)
        await asyncio.sleep(_config["process_interval_sec"])