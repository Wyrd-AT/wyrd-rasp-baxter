# ==============================================================================
# ARQUIVO: aggregator.py (VERSÃO DEFINITIVA E ROBUSTA)
# FUNÇÃO:  Cérebro do sistema RTLS. Processa dados brutos de sinal, aplica
#          regras de negócio e determina a localização final dos ativos.
# LÓGICA DE PENDENTES, SAÍDAS E CANCELAMENTOS CORRIGIDA.
# ==============================================================================

import asyncio
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session, joinedload

FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))

from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting, ReceivedEvent
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
from .dispatcher import dispatch_event
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHES GLOBAIS ---
_esp_map = {}
_asset_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()
_wifi_presence_cache = {}

_handshake_confirmed_assets = set()
_handshake_invalidated_assets = set()
_last_handshake_success = {}
HANDSHAKE_FAILURE_TIMEOUT_SEC = 180 # Ex: 3 minutos

SIGNAL_LOG_INTERVAL_SEC = 15.0
_last_signal_log_times_per_esp = {}

# --- CONFIGURAÇÕES ---
_config = {
    "process_interval_sec": float(settings.get('process_interval_sec', 2.0)),
    "reading_timeout_sec": int(settings.get('reading_timeout_sec', 10)),
    "default_rssi_threshold": -75,
    "inertia_entrada_ms": 3000,
    "inertia_saida_ms": 10000,
    "ema_alpha": float(settings.get('ema_alpha', 0.4)),
    "max_assets_per_room": 1
}
PENDING_WARNING_TIMEOUT_SEC = int(settings.get('pending_warning_timeout_sec', 300))
PENDING_EXPIRATION_TIMEOUT_SEC = int(settings.get('pending_expiration_timeout_sec', 900))
DISAPPEARANCE_TOLERANCE_CYCLES = int(settings.get('disappearance_tolerance_cycles', 10))
WIFI_FAILURE_INERTIA_SEC = int(settings.get('wifi_failure_inertia_sec', 180))

# --- CLASSE DE ESTADO DO ATIVO ---
class AssetState:
    def __init__(self, mac):
        self.mac = mac; self.readings = {}; self.last_known_ema = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000}
        self.last_known_wifi_signal = None; self.candidate_quarto_id = None
        self.candidate_since = None; self.disappeared_since = None
        self.disappearance_count = 0; 
        self.wifi_unseen_since = None

    def update_reading(self, esp_id, rssi, timestamp, wifi_signal=None):
        old_ema = self.readings.get(esp_id, {}).get("ema_rssi", rssi); alpha = _config["ema_alpha"]
        new_ema = (rssi * alpha) + (old_ema * (1 - alpha))
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp, "ema_rssi": new_ema, "wifi_signal": wifi_signal}
        self.last_known_ema[esp_id] = new_ema; self.disappearance_count = 0
        if wifi_signal is not None: self.last_known_wifi_signal = wifi_signal

    def cleanup_old_readings(self):
        now = time.time()
        self.readings = {k: v for k, v in self.readings.items() if now - v["timestamp"] < _config["reading_timeout_sec"]}
        return bool(self.readings)

# --- FUNÇÕES DE INTERFACE E CONTROLE ---
def update_wifi_presence_cache(latest_cache: dict):
    global _wifi_presence_cache
    _wifi_presence_cache = latest_cache

def clear_asset_candidate_state(mac_beacon_to_clear: str):
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        state.candidate_quarto_id = None; state.candidate_since = None
        state.wifi_unseen_since = None

        _handshake_confirmed_assets.discard(mac_beacon_to_clear)
        _handshake_invalidated_assets.discard(mac_beacon_to_clear)
        _last_handshake_success.pop(mac_beacon_to_clear, None) # Remove o timer
        
        logger.info(f"Estado de memória para o ativo {mac_beacon_to_clear} foi limpo.")
        return True
    return False

def update_asset_cache(mac_beacon: str, new_quarto_id: int | None):
    if mac_beacon in _asset_map: _asset_map[mac_beacon]["quarto_id"] = new_quarto_id

def flag_for_reload():
    _config_needs_reload.set()

# --- FUNÇÕES INTERNAS DO MOTOR RTLS ---
async def _consume_scan_data_queue():
    db = None
    try:
        while not scan_data_queue.empty():
            item = await scan_data_queue.get()
            esp_id_from_item = item.get("esp_id")
            if esp_id_from_item:
                now = time.time() 
                last_log_time = _last_signal_log_times_per_esp.get(esp_id_from_item, 0) 

                if (now - last_log_time) > SIGNAL_LOG_INTERVAL_SEC: 
                    signal_logger.info(json.dumps(item)) 
                    _last_signal_log_times_per_esp[esp_id_from_item] = now 
            esp_id, payload = item.get("esp_id"), item.get("payload", {})
            beacons_obj = payload.get("b", {}); wifi_signal = payload.get("w")
            for mac, rssi in beacons_obj.items():
                mac = mac.lower()
                if not mac or mac not in _asset_map: continue
                if mac not in _asset_realtime_state: _asset_realtime_state[mac] = AssetState(mac)
                _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time(), wifi_signal)
                asset_info = _asset_map.get(mac)
                if asset_info and asset_info.get("status") == 'Offline':
                    if db is None: db = SessionLocal()
                    asset_db = db.query(Asset).get(asset_info.get("id"))
                    if asset_db: asset_db.status = 'Online'
                    _asset_map[mac]['status'] = 'Online'
    finally:
        if db: db.commit(); db.close()

async def _processar_localizacoes():
    """O coração da lógica de localização, com a máquina de estados final e robusta."""
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    now = time.time()
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            
            # --- CÁLCULO DE CANDIDATO (FEITO PARA TODOS OS ATIVOS COM SINAL) ---
            candidate_quarto_id = None
            strongest_candidate = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000, "quarto_id": None, "wifi_signal": None}
            if state.cleanup_old_readings():
                for esp_id, reading in state.readings.items():
                    if esp_id not in _esp_map: continue
                    q_id, q_rssi = _esp_map[esp_id]
                    if any(om != mac and oa.get("quarto_id") == q_id for om, oa in _asset_map.items()): continue

                    if any(om != mac and o_state.pending_quarto_id == q_id for om, o_state in _asset_realtime_state.items()): continue
                    
                    threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                    if reading["ema_rssi"] > threshold and reading["ema_rssi"] > strongest_candidate["ema_rssi"]:
                        strongest_candidate.update({"esp_id": esp_id, "rssi": reading["rssi"], "ema_rssi": reading["ema_rssi"], "quarto_id": q_id, "wifi_signal": reading.get("wifi_signal")})
                candidate_quarto_id = strongest_candidate["quarto_id"]
                if state.readings:
                    top_esp, top_read = max(state.readings.items(), key=lambda i: i[1]['ema_rssi'])
                    state.last_strongest_signal = {"esp_id": top_esp, "rssi": top_read['rssi'], "ema_rssi": top_read['ema_rssi']}

            # ESTADO 2: DESAPARECIDO (SEM SINAL BLE HÁ MUITO TEMPO)
            if not state.readings:
                state.disappearance_count += 1
                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    asset_info = _asset_map.get(mac, {})
                    if asset_info.get("status") == 'Online':
                        if asset_info.get("quarto_id") is not None:
                            changes_to_commit.append({"asset_id": asset_info.get("id"), "new_quarto_id": None, "source_esp_id": "server_disappearance", "details": "Ativo desapareceu do radar BLE."})
                        asset_db = db.query(Asset).get(asset_info.get("id"));
                        if asset_db: asset_db.status = 'Offline'
                        _asset_map[mac]['status'] = 'Offline'
                    del _asset_realtime_state[mac]
                continue
            
            asset_info = _asset_map.get(mac, {}); asset_id = asset_info.get("id")
            quarto_id_atual = asset_info.get("quarto_id")

            # ESTADO 3: ALOCADO (DENTRO DE UM QUARTO) - LÓGICA REFEITA
            if quarto_id_atual is not None:
                # Verificação 1: O sinal BLE ainda é consistente com o quarto atual?
                if candidate_quarto_id == quarto_id_atual:
                    state.disappeared_since = None # Se sim, reseta a inércia de saída do BLE.
                else:
                    if state.disappeared_since is None:
                        logger.info(f"[BLE] Sinal para {mac} no quarto {quarto_id_atual} inconsistente. Iniciando inércia de saída.")
                        state.disappeared_since = now
                    elif (now - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        logger.info(f"[BLE] EVENTO OUT (INÉRCIA): Ativo {mac} removido do Quarto {quarto_id_atual} por sinal BLE fraco.")
                        changes_to_commit.append({"asset_id": asset_id, "new_quarto_id": None, "source_esp_id": "server_inertia_out", "rssi": state.last_strongest_signal.get('rssi'), "details": "Sinal BLE inconsistente com o quarto atual.", "action": "OUT"})
                        clear_asset_candidate_state(mac)
                        continue # Pula para o próximo ativo

                # Verificação 2: O ativo ainda está respondendo ao handshake de presença?
                
                # --- NOVA VERIFICAÇÃO DE ALTA PRIORIDADE ---
                if mac in _handshake_invalidated_assets:
                    logger.error(f"[HANDSHAKE] EVENTO OUT (CALLBACK FALSE): Ativo {mac} removido do quarto {quarto_id_atual} por resposta explícita 'FALSE'.")
                    changes_to_commit.append({
                        "asset_id": asset_id, "new_quarto_id": None,
                        "source_esp_id": "aggregator_handshake_monitor",
                        "details": "Removido por confirmação explícita de ausência (callback 'FALSE').",
                        "action": "OUT"
                    })
                    _handshake_invalidated_assets.remove(mac) # Consome o flag
                    clear_asset_candidate_state(mac)
                    continue # Processo concluído para este ativo

                # A lógica de timeout original continua abaixo, como um fallback.
                last_success = _last_handshake_success.get(mac, 0)
                
                # Se nunca tivemos um sucesso ou o último foi há muito tempo, remove o ativo.
                if last_success != 0 and (now - last_success) > HANDSHAKE_FAILURE_TIMEOUT_SEC:
                    logger.error(f"[HANDSHAKE] EVENTO OUT (TIMEOUT): Ativo {mac} sem resposta de handshake por mais de {HANDSHAKE_FAILURE_TIMEOUT_SEC}s. Forçando remoção.")
                    
                    changes_to_commit.append({
                        "asset_id": asset_id, "new_quarto_id": None,
                        "source_esp_id": "aggregator_handshake_monitor",
                        "details": f"Removido por ausência de resposta ao handshake superior a {HANDSHAKE_FAILURE_TIMEOUT_SEC}s.",
                        "action": "OUT"
                    })
                    clear_asset_candidate_state(mac)
                    continue # Pula para o próximo ativo

            # ESTADO 4: LIVRE (FORA DE UM QUARTO)
            if quarto_id_atual is None and candidate_quarto_id is not None:
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = now
                
                # Se a inércia de entrada for cumprida, marca o ativo como PENDENTE no DB
                if state.candidate_since and (now - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    logger.info(f"NOVA ENTRADA DETECTADA: Ativo {mac} -> Quarto {candidate_quarto_id}. Marcando como 'Pendente' no DB.")
                    
                    changes_to_commit.append({
                        "asset_id": asset_id,
                        "new_quarto_id": candidate_quarto_id,
                        "location_status": "Pendente", # Define o novo status
                        "action": "GET",
                        "status": "Pendente-Inicial",
                        "details": "Ativo detectado por BLE, iniciando processo de confirmação.",
                        "quarto_context_id": candidate_quarto_id
                    })
                    
                    # Limpa o estado de candidato da memória, pois agora o DB é a fonte da verdade
                    state.candidate_since = None
                    state.candidate_quarto_id = None
            else:
                if state.candidate_quarto_id is not None:
                    logger.info(f"Ativo {mac} perdeu seu sinal de candidato para o quarto {state.candidate_quarto_id}. Abortando processo de entrada.")
                    clear_asset_candidate_state(mac)

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit, _asset_map)
        db.commit()
    finally:
        db.close()

def confirm_asset_by_handshake(mac_beacon: str):
    """
    Função chamada pelo endpoint da API em main.py para registrar
    uma confirmação de presença bem-sucedida.
    """
    if mac_beacon:
        logger.info(f"[HANDSHAKE-STATE] Ativo {mac_beacon} confirmado via handshake.")
        _handshake_confirmed_assets.add(mac_beacon)
        _last_handshake_success[mac_beacon] = time.time()

def invalidate_asset_by_handshake(mac_beacon: str):
    """Função chamada pelo endpoint da API para registrar uma invalidação explícita."""
    if mac_beacon:
        logger.warning(f"[HANDSHAKE-STATE] Ativo {mac_beacon} invalidado via callback 'FALSE'.")
        _handshake_invalidated_assets.add(mac_beacon)

def _load_maps_from_db():
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        assets = db.query(Asset).all()
        _asset_map = { a.mac_beacon: {"id": a.id, "nome_ativo": a.nome_ativo, "modelo": a.modelo, "quarto_id": a.quarto_id, "wifi_mac": a.mac_address, "status": a.status, "location_status": a.location_status } for a in assets }
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        _config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        _config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        _config["inertia_saida_ms"] = int(settings_from_db.get("inertia_saida", _config["inertia_saida_ms"]))
        _config["max_assets_per_room"] = int(settings_from_db.get("max_assets_per_room", _config["max_assets_per_room"]))
        _config["ema_alpha"] = float(settings_from_db.get("ema_alpha", _config["ema_alpha"]))
        logger.info(f"[RTLS] Cache (re)carregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos.")
    finally:
        db.close()

async def main_aggregator_loop():
    logger.info("[RTLS] Motor de localização iniciado.")
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