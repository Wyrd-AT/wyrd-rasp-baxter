# aggregator.py (Versão Otimizada com Cache e Lógica Refinada)
import asyncio
import time
import json
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting
from .services import update_asset_assignment
from .mqtt_client import scan_data_queue
import logging

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHE EM MEMÓRIA PARA DADOS DO DB ---
_esp_map = {}
_asset_map = {}
_config_needs_reload = asyncio.Event() # Evento para sinalizar necessidade de recarga

# --- O "Mapa" em Memória do Estado dos Ativos ---
_asset_realtime_state = {}

# --- Configurações Padrão do Motor RTLS ---
_config = {
    "process_interval_sec": 2.0,
    "reading_timeout_sec": 10,
    "default_rssi_threshold": -75,
    "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000,
    "inertia_saida_ms": 30000
}

class AssetState:
    """Uma classe para guardar o estado completo de um ativo em tempo real."""
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000}
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.disappeared_since = None

    def update_reading(self, esp_id, rssi, timestamp):
        """Atualiza a leitura de uma ESP e reseta o temporizador de desaparecimento."""
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp}
        self.disappeared_since = None

    def cleanup_old_readings(self):
        """Remove leituras com mais de X segundos e retorna se ainda há leituras válidas."""
        now = time.time()
        self.readings = {
            esp_id: data for esp_id, data in self.readings.items()
            if now - data["timestamp"] < _config["reading_timeout_sec"]
        }
        return bool(self.readings)

async def _consume_scan_data_queue():
    """Consome a fila do MQTT e atualiza os objetos de estado em memória."""
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        signal_logger.info(json.dumps(item))
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

async def _processar_localizacoes():
    """O cérebro do RTLS: analisa o estado e toma decisões de localização."""
    # AGORA USAMOS OS MAPAS EM CACHE! Sem acesso ao DB aqui.
    if not _esp_map or not _asset_map:
        logger.warning("[RTLS] Mapas de ESPs ou Ativos vazios. A aguardar carregamento.")
        return

    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_id, quarto_id_atual = _asset_map.get(mac, (None, None))
            if not asset_id:
                continue

            # --- LÓGICA DE SAÍDA REFINADA ---
            if not state.cleanup_old_readings():
                if quarto_id_atual is not None:
                    if state.disappeared_since is None:
                        state.disappeared_since = time.time()
                    
                    if (time.time() - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        del _asset_realtime_state[mac]
                        
                        last_rssi = state.last_strongest_signal.get("rssi", -1000)
                        # Usamos o ID da última ESP que viu o ativo, em vez de "server"
                        last_esp_id = state.last_strongest_signal.get("esp_id", "server_timeout")
                        
                        details = f"Ativo desapareceu (Inércia Saída: {_config['inertia_saida_ms']}ms)"
                        
                        # Esta chamada agora terá a ESP correta para qualquer uma das lógicas
                        await update_asset_assignment(db, asset_id, None, last_esp_id, last_rssi, details)
                else:
                    del _asset_realtime_state[mac]
                continue
            
            # --- Lógica de Entrada (Localização) ---
            strongest_candidate = {"esp_id": None, "rssi": -1000, "quarto_id": None}
            for esp_id, reading in state.readings.items():
                if esp_id not in _esp_map: continue
                
                quarto_id, custom_rssi = _esp_map[esp_id]
                threshold = custom_rssi if custom_rssi is not None else _config["default_rssi_threshold"]
                
                if reading["rssi"] > threshold and reading["rssi"] > strongest_candidate["rssi"]:
                    strongest_candidate = {"esp_id": esp_id, "rssi": reading["rssi"], "quarto_id": quarto_id}
            
            state.last_strongest_signal = {"esp_id": strongest_candidate["esp_id"], "rssi": strongest_candidate["rssi"]}
            candidate_quarto_id = strongest_candidate["quarto_id"]

            # Se não há candidato forte o suficiente, reinicia a inércia
            if not candidate_quarto_id:
                state.candidate_quarto_id = None
                state.candidate_since = None
                continue

            # Se o candidato é diferente do que estávamos a avaliar...
            if candidate_quarto_id != state.candidate_quarto_id:
                # REGRA DE HISTERESE: Evita "ping-pong" entre quartos
                if quarto_id_atual is not None:
                    # Procura o RSSI do quarto atual para comparar
                    current_reading_rssi = next((r["rssi"] for e, r in state.readings.items() if _esp_map.get(e, (None,None))[0] == quarto_id_atual), -1000)
                    if strongest_candidate["rssi"] < (current_reading_rssi + _config["conflict_margin_db"]):
                        logger.debug(f"Ativo {mac}: candidato {candidate_quarto_id} ({strongest_candidate['rssi']}dBm) ignorado por margem de conflito. Atual: {quarto_id_atual} ({current_reading_rssi}dBm).")
                        continue # Ignora este candidato, pois não é "claramente" mais forte
                
                # Inicia o temporizador de inércia para o novo candidato
                state.candidate_quarto_id = candidate_quarto_id
                state.candidate_since = time.time()
                logger.debug(f"Ativo {mac}: novo candidato é o quarto {candidate_quarto_id}. A iniciar inércia de entrada...")

            # Se o tempo de inércia passou e a localização precisa de ser atualizada...
            if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                if state.candidate_quarto_id != quarto_id_atual:
                    details = f"Localizado via {strongest_candidate['esp_id']} com RSSI {strongest_candidate['rssi']}"
                    logger.info(f"Ativo {mac} movido para quarto {state.candidate_quarto_id}. Detalhes: {details}")
                    await update_asset_assignment(db, asset_id, state.candidate_quarto_id, strongest_candidate['esp_id'], strongest_candidate['rssi'], details)
                    # Reseta a inércia para evitar múltiplas escritas
                    state.candidate_since = None 
    finally:
        db.close()

def _load_maps_from_db():
    """Carrega os mapas de configuração e ativos da base de dados para o cache."""
    global _esp_map, _asset_map
    db = SessionLocal()
    try:
        # Carrega e transforma os dados para o cache
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        assets = db.query(Asset).all()
        _asset_map = {a.mac_beacon: (a.id, a.quarto_id) for a in assets}
        
        # Carrega configurações globais
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        _config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        _config["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", _config["conflict_margin_db"]))
        _config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        _config["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", _config["inertia_saida_ms"]))
        
        logger.info(f"[RTLS] Cache (re)carregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos. Configs: {str(_config)}")
    finally:
        db.close()

# Função pública para ser chamada a partir do main.py
def flag_for_reload():
    """Sinaliza ao agregador que as configurações precisam ser recarregadas."""
    _config_needs_reload.set()

async def main_aggregator_loop():
    """O ciclo de relógio do motor RTLS, orquestrando todas as operações."""
    logger.info("[RTLS] Motor de localização INTELIGENTE iniciado.")
    
    # Carga inicial
    _load_maps_from_db()
    
    while True:
        try:
            # Verifica se uma recarga foi solicitada
            if _config_needs_reload.is_set():
                _load_maps_from_db()
                _config_needs_reload.clear() # Limpa o sinalizador
            
            await _consume_scan_data_queue()
            await _processar_localizacoes()
            
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico no loop principal: {e}", exc_info=True)
            
        await asyncio.sleep(_config["process_interval_sec"])