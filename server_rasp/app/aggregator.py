# aggregator.py (Versão Final com Média Móvel Exponencial)
import asyncio
import time
import json
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
import logging
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHE EM MEMÓRIA ---
_esp_map = {}
_asset_map = {}
_config_needs_reload = asyncio.Event()
_asset_realtime_state = {}

SIGNAL_LOG_INTERVAL_SEC = 15.0  
_last_signal_log_times_per_esp = {}

# --- Configurações Padrão ---
_config = {
    "process_interval_sec": 2.0,
    "reading_timeout_sec": 10,
    "default_rssi_threshold": -75,
    "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000,
    "inertia_saida_ms": 10000,
    "ema_alpha": 0.4,
    "disappearance_tolerance_cycles": 10 
}

class AssetState:
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}
        self.last_known_ema = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000}
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.disappeared_since = None
        self.weak_signal_since = None
        self.disappearance_count = 0

    def update_reading(self, esp_id, rssi, timestamp):
        """
        Atualiza a leitura de uma ESP e calcula a Média Móvel Exponencial (EMA).
        """
        old_ema = self.readings.get(esp_id, {}).get("ema_rssi", rssi)
        alpha = _config["ema_alpha"]
        new_ema = (rssi * alpha) + (old_ema * (1 - alpha))
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp, "ema_rssi": new_ema}
        self.last_known_ema[esp_id] = new_ema
        self.disappeared_since = None
        self.disappearance_count = 0 

    def cleanup_old_readings(self):
        now = time.time()
        self.readings = {
            esp_id: data for esp_id, data in self.readings.items()
            if now - data["timestamp"] < _config["reading_timeout_sec"]
        }
        return bool(self.readings)

async def _consume_scan_data_queue():
    global _last_signal_log_times_per_esp
    db = None

    try:
        while not scan_data_queue.empty():
            item = await scan_data_queue.get()

            esp_id = item.get("esp_id")
            if not esp_id:
                continue    

            now = time.time()
            last_log_time = _last_signal_log_times_per_esp.get(esp_id, 0)

            if (now - last_log_time) > SIGNAL_LOG_INTERVAL_SEC:
                signal_logger.info(json.dumps(item))
                _last_signal_log_times_per_esp[esp_id] = now

            
            payload = item.get("payload", {})

            beacons_obj = payload.get("b", {})

            for mac, rssi in beacons_obj.items():
                mac = mac.lower()
                if not mac or mac not in _asset_map: continue

                asset_info = _asset_map.get(mac)
                
                if asset_info.get("status") == 'Offline':
                    if db is None: db = SessionLocal() 
                    asset_db = db.query(Asset).get(asset_info.get("id"))
                    if asset_db:
                        asset_db.status = 'Online'
                        _asset_map[mac]['status'] = 'Online'
                
                if mac not in _asset_realtime_state:
                    _asset_realtime_state[mac] = AssetState(mac)
                
                _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time())
    finally:
        if db:
            db.commit()
            db.close()

async def _processar_localizacoes():
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {})
            asset_id = asset_info.get("id")
            quarto_id_atual = asset_info.get("quarto_id")

            if not asset_id: continue

            if not state.cleanup_old_readings():
                state.disappearance_count += 1
                
                logger.debug(f"Ativo {mac} sem sinal. Contagem de desaparecimento: {state.disappearance_count}/{_config['disappearance_tolerance_cycles']}.")

                if state.disappearance_count >= _config['disappearance_tolerance_cycles']:
                    
                    if quarto_id_atual is not None:
                        logger.info(f"EVENTO OUT (CICLOS): Ativo {mac} desapareceu consistentemente. Removendo do Quarto {quarto_id_atual}.")
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": None,
                            "source_esp_id": state.last_strongest_signal.get("esp_id") or "server_disappearance",
                            "rssi": state.last_strongest_signal.get("rssi", -1000),
                            "details": f"Ativo desapareceu do radar BLE por {_config['disappearance_tolerance_cycles']} ciclos."
                        })
                    
                    if asset_info.get("status") == 'Online':
                        logger.warning(f"Ativo '{mac}' desapareceu consistentemente. Marcando como Offline.")
                        asset_db = db.query(Asset).get(asset_id)
                        if asset_db:
                            asset_db.status = 'Offline'
                        if mac in _asset_map:
                            _asset_map[mac]['status'] = 'Offline'

                    del _asset_realtime_state[mac]
                continue 
            
            # Encontra o candidato mais forte baseado na EMA
            strongest_candidate = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000, "quarto_id": None}
            for esp_id, reading in state.readings.items():
                if esp_id not in _esp_map: continue
                q_id, q_rssi = _esp_map[esp_id]
                threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                # A decisão de ser um candidato agora usa a EMA
                if reading["ema_rssi"] > threshold and reading["ema_rssi"] > strongest_candidate["ema_rssi"]:
                    strongest_candidate = {
                        "esp_id": esp_id, 
                        "rssi": reading["rssi"],      # Guarda o RSSI bruto para log
                        "ema_rssi": reading["ema_rssi"],# Usa a EMA para decisão
                        "quarto_id": q_id
                    }
            
            # Atualiza o sinal mais forte geral (para logs e referência)
            top_esp, top_read = max(state.readings.items(), key=lambda i: i[1]['ema_rssi'])
            state.last_strongest_signal = {"esp_id": top_esp, "rssi": top_read['rssi'], "ema_rssi": top_read['ema_rssi']}

            candidate_quarto_id = strongest_candidate["quarto_id"]

            if quarto_id_atual is not None and not candidate_quarto_id:
                if state.weak_signal_since is None: state.weak_signal_since = time.time()
                elif (time.time() - state.weak_signal_since) * 1000 > _config["inertia_saida_ms"]:
                    logger.info(f"EVENTO OUT (SINAL FRACO): Ativo {mac} marcado para remoção do Quarto {quarto_id_atual}.")
                    # Adiciona a mudança à lista
                    changes_to_commit.append({
                        "asset_id": asset_id, "new_quarto_id": None,
                        "source_esp_id": state.last_strongest_signal['esp_id'],
                        "rssi": state.last_strongest_signal['rssi'],
                        "details": f"Sinal (EMA) permaneceu fraco por {_config['inertia_saida_ms']}ms"
                    })
                    if mac in _asset_map: _asset_map[mac]['quarto_id'] = None
                    state.weak_signal_since = None
                    continue
            elif state.weak_signal_since is not None: state.weak_signal_since = None

            if candidate_quarto_id == quarto_id_atual:
                state.candidate_quarto_id = quarto_id_atual
                state.candidate_since = None
                continue

            if candidate_quarto_id != state.candidate_quarto_id:
                if quarto_id_atual is not None and candidate_quarto_id is not None:
                    esps_no_quarto_atual = [esp for esp, (q_id, _) in _esp_map.items() if q_id == quarto_id_atual]
                    
                    leituras_ao_vivo = [r["ema_rssi"] for e, r in state.readings.items() if e in esps_no_quarto_atual]
                    
                    if leituras_ao_vivo:
                        rssi_para_comparacao = max(leituras_ao_vivo)
                    else:
                        emas_conhecidos = [ema for esp, ema in state.last_known_ema.items() if esp in esps_no_quarto_atual]
                        rssi_para_comparacao = max(emas_conhecidos) if emas_conhecidos else -1000
                    
                    if strongest_candidate["ema_rssi"] < (rssi_para_comparacao + _config["conflict_margin_db"]):
                        #logger.info(f"CONFLITO: Ativo {mac}: Troca de Q{quarto_id_atual} para Q{candidate_quarto_id} NEGADA. Sinal EMA Cand: {strongest_candidate['ema_rssi']:.1f}dBm vs Base Atual: {rssi_para_comparacao:.1f}dBm + Margem: {_config['conflict_margin_db']}dBm")
                        continue
                
                state.candidate_quarto_id = candidate_quarto_id
                state.candidate_since = time.time()

            if state.candidate_since and state.candidate_quarto_id is not None:
                if (time.time() - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == candidate_quarto_id and state.candidate_quarto_id != quarto_id_atual:
                        logger.info(f"EVENTO IN: Ativo {mac} confirmado no Quarto {state.candidate_quarto_id}.")
                        # Adiciona a mudança à lista
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": state.candidate_quarto_id,
                            "source_esp_id": strongest_candidate['esp_id'],
                            "rssi": strongest_candidate['rssi'],
                            "details": f"Localizado via {strongest_candidate['esp_id']} com RSSI Bruto {strongest_candidate['rssi']} (EMA {strongest_candidate['ema_rssi']:.1f})"
                        })
                        if mac in _asset_map:
                            _asset_map[mac]["quarto_id"] = state.candidate_quarto_id
                        state.candidate_since = None
        
        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
        db.commit()
    finally:
        db.close()

def clear_asset_state(mac_beacon_to_clear: str):
    """Função de controle para limpar o estado de um ativo, chamada externamente."""
    if mac_beacon_to_clear in _asset_realtime_state:
        del _asset_realtime_state[mac_beacon_to_clear]
        logger.info(f"Estado de memória para o ativo {mac_beacon_to_clear} foi limpo.")
        return True
    return False

def _load_maps_from_db():
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        assets = db.query(Asset).all()
        # --- ATUALIZAÇÃO AQUI: Guardamos o status também ---
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, 
                "quarto_id": a.quarto_id,
                "status": a.status
            } for a in assets
        }

        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        
        # Sobrescreve as configurações padrão com as do banco de dados
        _config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        _config["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", _config["conflict_margin_db"]))
        _config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        _config["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", _config["inertia_saida_ms"]))
        _config["ema_alpha"] = float(settings_from_db.get("ema_alpha", _config["ema_alpha"]))
        
        # (NOVO) Carrega as configurações do config.ini, se não estiverem no banco de dados
        _config["process_interval_sec"] = float(settings.get('process_interval_sec', _config["process_interval_sec"]))
        _config["reading_timeout_sec"] = int(settings.get('reading_timeout_sec', _config["reading_timeout_sec"]))
        _config["disappearance_tolerance_cycles"] = int(settings.get('disappearance_tolerance_cycles', _config["disappearance_tolerance_cycles"]))
        
        logger.info(f"[RTLS] Cache (re)carregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos. Configs: {str(_config)}")
    finally:
        db.close()

def flag_for_reload():
    _config_needs_reload.set()

async def main_aggregator_loop():
    logger.info("[RTLS] Motor de localização (v. com EMA) iniciado.")
    _load_maps_from_db()
    while True:
        try:
            if _config_needs_reload.is_set():
                _load_maps_from_db()
                _config_needs_reload.clear()
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico no loop principal: {e}", exc_info=True)
        await asyncio.sleep(_config["process_interval_sec"])