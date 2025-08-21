# aggregator.py (Versão Final com Média Móvel Exponencial)
import asyncio
import time
import json
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
import logging

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHE EM MEMÓRIA ---
_esp_map = {}
_asset_map = {}
_config_needs_reload = asyncio.Event()
_asset_realtime_state = {}

# --- Configurações Padrão ---
_config = {
    "process_interval_sec": 2.0,
    "reading_timeout_sec": 10,
    "default_rssi_threshold": -75,
    "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000,
    "inertia_saida_ms": 10000,
    "ema_alpha": 0.4 
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

    def update_reading(self, esp_id, rssi, timestamp):
        """
        Atualiza a leitura de uma ESP e calcula a Média Móvel Exponencial (EMA).
        """
        # Se for a primeira leitura desta ESP, a EMA inicial é o próprio RSSI.
        old_ema = self.readings.get(esp_id, {}).get("ema_rssi", rssi)
        
        # Fórmula da Média Móvel Exponencial
        alpha = _config["ema_alpha"]
        new_ema = (rssi * alpha) + (old_ema * (1 - alpha))

        # Guarda o valor bruto (rssi) e o valor suavizado (ema_rssi)
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp, "ema_rssi": new_ema}
        
        self.last_known_ema[esp_id] = new_ema

        self.disappeared_since = None

    def cleanup_old_readings(self):
        now = time.time()
        self.readings = {
            esp_id: data for esp_id, data in self.readings.items()
            if now - data["timestamp"] < _config["reading_timeout_sec"]
        }
        return bool(self.readings)

async def _consume_scan_data_queue():
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        signal_logger.info(json.dumps(item))
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons, timestamp = payload.get("beacons", []), payload.get("timestamp", time.time())
        for beacon in beacons:
            mac = beacon.get("mac", "").lower()
            if not mac: continue
            if mac not in _asset_realtime_state:
                _asset_realtime_state[mac] = AssetState(mac)
            _asset_realtime_state[mac].update_reading(esp_id, beacon.get("rssi"), timestamp)

async def _processar_localizacoes():
    if not _esp_map or not _asset_map: return

    # 1. Cria uma lista vazia para colecionar as mudanças do ciclo atual.
    changes_to_commit = []

    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_id, quarto_id_atual = _asset_map.get(mac, (None, None))
            if not asset_id: continue

            # --- Lógica de SAÍDA POR TIMEOUT ---
            if not state.cleanup_old_readings():
                if quarto_id_atual is not None:
                    if state.disappeared_since is None: state.disappeared_since = time.time()
                    if (time.time() - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        logger.info(f"EVENTO OUT (TIMEOUT): Ativo {mac} marcado para remoção do Quarto {quarto_id_atual}.")
                        # Adiciona a mudança à lista, em vez de chamar o serviço
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None,
                            "source_esp_id": state.last_strongest_signal.get("esp_id") or "server_timeout",
                            "rssi": state.last_strongest_signal.get("rssi", -1000),
                            "details": f"Ativo desapareceu (Inércia Saída: {_config['inertia_saida_ms']}ms)"
                        })
                        if mac in _asset_map: _asset_map[mac] = (asset_id, None)
                        del _asset_realtime_state[mac]
                else:
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
                    if mac in _asset_map: _asset_map[mac] = (asset_id, None)
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
                        if mac in _asset_map: _asset_map[mac] = (asset_id, state.candidate_quarto_id)
                        state.candidate_since = None
        
        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
    finally:
        db.close()

def _load_maps_from_db():
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        assets = db.query(Asset).all()
        _asset_map = {a.mac_beacon: (a.id, a.quarto_id) for a in assets}
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        
        new_config = _config.copy()
        new_config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        new_config["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", _config["conflict_margin_db"]))
        new_config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        new_config["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", _config["inertia_saida_ms"]))
        # Carrega o novo parâmetro também, se existir no DB
        new_config["ema_alpha"] = float(settings_from_db.get("ema_alpha", _config["ema_alpha"]))
        _config = new_config
        
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