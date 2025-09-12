# aggregator.py (Versão com "Agregador Paciente" e "Guardião de Wi-Fi")

import asyncio
import time
import json
import logging
from datetime import datetime, timezone, timedelta

FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))

from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting, ReceivedEvent
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
from .dispatcher import dispatch_event
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# ===================================================================
# ARQUIVO MODIFICADO: aggregator.py
# ===================================================================

# --- CACHES GLOBAIS ---
_esp_map = {}
_asset_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()
_wifi_presence_cache = {} # O novo cache de Wi-Fi que será atualizado pelo Guardião

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
PENDING_WARNING_TIMEOUT_SEC = int(settings.get('pending_warning_timeout_sec', 300)) # 5 minutos
PENDING_EXPIRATION_TIMEOUT_SEC = int(settings.get('pending_expiration_timeout_sec', 900)) # 15 minutos
DISAPPEARANCE_TOLERANCE_CYCLES = int(settings.get('disappearance_tolerance_cycles', 10))

# --- CLASSE DE ESTADO DO ATIVO (sem a lógica de quarentena) ---
class AssetState:
    def __init__(self, mac):
        self.mac = mac; self.readings = {}; self.last_known_ema = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000}
        self.last_known_wifi_signal = None; self.candidate_quarto_id = None
        self.candidate_since = None; self.disappeared_since = None
        self.disappearance_count = 0; self.pending_wifi_check_since = None
        self.pending_quarto_id = None; self.pending_event_details = {}
        self.warning_issued = False

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
    """Permite que o Guardião de Wi-Fi atualize o cache local do agregador."""
    global _wifi_presence_cache
    _wifi_presence_cache = latest_cache

def clear_asset_candidate_state(mac_beacon_to_clear: str):
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        state.candidate_quarto_id = None; state.candidate_since = None
        state.pending_wifi_check_since = None; state.pending_quarto_id = None
        state.pending_event_details = {}; state.warning_issued = False
        logger.info(f"Estado de memória para o ativo {mac_beacon_to_clear} foi limpo.")
        return True
    return False

def update_asset_cache(mac_beacon: str, new_quarto_id: int | None):
    if mac_beacon in _asset_map: _asset_map[mac_beacon]["quarto_id"] = new_quarto_id

def flag_for_reload():
    _config_needs_reload.set()

# --- FUNÇÕES INTERNAS DO MOTOR RTLS ---
async def _consume_scan_data_queue():
    # (Esta função permanece inalterada)
    db = None
    try:
        while not scan_data_queue.empty():
            item = await scan_data_queue.get()
            signal_logger.info(json.dumps(item))
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
    """O coração da lógica de localização, reescrito para usar o cache de Wi-Fi."""
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    now = time.time()
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            
            # --- ESTÁGIO 1: PROCESSAR ATIVOS QUE JÁ ESTÃO PENDENTES ---
            if state.pending_wifi_check_since is not None:
                # Consulta o cache de Wi-Fi (não faz uma nova verificação)
                if _wifi_presence_cache.get(mac, False):
                    logger.info(f"EVENTO CONFIRMADO (Wi-Fi OK): Ativo {mac} confirmado no Quarto {state.pending_quarto_id} via cache.")
                    changes_to_commit.append({**state.pending_event_details, "new_quarto_id": state.pending_quarto_id})
                    clear_asset_candidate_state(mac)
                    continue

                # Se o Wi-Fi ainda não apareceu, verifica os timeouts
                if (now - state.pending_wifi_check_since) > PENDING_WARNING_TIMEOUT_SEC and not state.warning_issued:
                    logger.warning(f"EVENTO ALERTA (PENDENTE): Wi-Fi para {mac} ausente por mais de {PENDING_WARNING_TIMEOUT_SEC}s.")
                    quarto_pendente = db.query(Quarto).get(state.pending_quarto_id)
                    warning_event = ReceivedEvent(esp_id=state.pending_event_details.get("source_esp_id", "aggregator"), ativo=mac, quarto_nome=quarto_pendente.nome if quarto_pendente else "N/A", action="ALERTA", status="OK", status_detail=f"Ativo detectado, mas Wi-Fi ausente por mais de {PENDING_WARNING_TIMEOUT_SEC}s.", rssi=state.pending_event_details.get("rssi"), data_on=datetime.now(timezone.utc), raw={})
                    db.add(warning_event)
                    state.warning_issued = True

                elif (now - state.pending_wifi_check_since) > PENDING_EXPIRATION_TIMEOUT_SEC:
                    logger.error(f"EVENTO VENCIDO (TIMEOUT): Ativo {mac} excedeu o tempo limite de espera pelo Wi-Fi.")
                    quarto_pendente = db.query(Quarto).get(state.pending_quarto_id)
                    failure_event = ReceivedEvent(esp_id=state.pending_event_details.get("source_esp_id", "aggregator"), ativo=mac, quarto_nome=quarto_pendente.nome if quarto_pendente else "N/A", action="GET", status="Vencido", status_detail=f"O Wi-Fi do ativo não foi detectado no tempo limite de {PENDING_EXPIRATION_TIMEOUT_SEC}s.", rssi=state.pending_event_details.get("rssi"), data_on=datetime.now(timezone.utc), raw={})
                    db.add(failure_event)
                    clear_asset_candidate_state(mac)
                
                continue # Pula para o próximo ativo enquanto estiver pendente

            # --- ESTÁGIO 2: PROCESSAR SAÍDAS E ENTRADAS ---
            if not state.cleanup_old_readings(): # Lógica de desaparecimento mantida
                state.disappearance_count += 1
                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    # (código para remover ativo desaparecido)
                    del _asset_realtime_state[mac]
                continue
            
            asset_info = _asset_map.get(mac, {}); asset_id = asset_info.get("id")
            quarto_id_atual = asset_info.get("quarto_id")

            strongest_candidate = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000, "quarto_id": None, "wifi_signal": None}
            # (Lógica para encontrar o sinal mais forte mantida)
            for esp_id, reading in state.readings.items():
                if esp_id not in _esp_map: continue
                q_id, q_rssi = _esp_map[esp_id]
                if any(om != mac and oa.get("quarto_id") == q_id for om, oa in _asset_map.items()): continue
                threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                if reading["ema_rssi"] > threshold and reading["ema_rssi"] > strongest_candidate["ema_rssi"]:
                    strongest_candidate.update({"esp_id": esp_id, "rssi": reading["rssi"], "ema_rssi": reading["ema_rssi"], "quarto_id": q_id, "wifi_signal": reading.get("wifi_signal")})
            candidate_quarto_id = strongest_candidate["quarto_id"]
            
            if quarto_id_atual is not None: # Lógica de saída mantida
                if candidate_quarto_id != quarto_id_atual:
                    if state.disappeared_since is None: state.disappeared_since = now
                    elif (now - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        changes_to_commit.append({"asset_id": asset_id, "new_quarto_id": None, "source_esp_id": "server_inertia_out", "details": "Sinal fraco."})
                else: state.disappeared_since = None
                continue

            if candidate_quarto_id is not None:
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = now
                
                if state.candidate_since and (now - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    if _wifi_presence_cache.get(mac, False):
                        logger.info(f"ENTRADA DIRETA: Ativo {mac} com Wi-Fi já presente. Confirmando entrada no Quarto {candidate_quarto_id}.")
                        changes_to_commit.append({"asset_id": asset_id, "new_quarto_id": candidate_quarto_id, "source_esp_id": strongest_candidate['esp_id'], "rssi": strongest_candidate['rssi'], "details": "Entrada direta com Wi-Fi pré-confirmado."})
                    else:
                        logger.info(f"EVENTO PENDENTE (em memória): Ativo {mac} -> Quarto {candidate_quarto_id}. Aguardando Wi-Fi.")
                        state.pending_quarto_id = candidate_quarto_id
                        state.pending_wifi_check_since = now
                        state.pending_event_details = {"asset_id": asset_id, "source_esp_id": strongest_candidate['esp_id'], "rssi": strongest_candidate['rssi'], "details": "Wi-Fi confirmado via cache."}
                    
                    state.candidate_since = None
                    state.candidate_quarto_id = None
            else:
                state.candidate_quarto_id = None
                state.candidate_since = None

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
        db.commit()
    finally:
        db.close()

# --- DEMAIS FUNÇÕES (sem alterações) ---
# (As funções _load_maps_from_db, get_pending_states_for_ui, main_aggregator_loop permanecem as mesmas)
def get_pending_states_for_ui():
    pending_list = []
    now = time.time()
    for mac, state in _asset_realtime_state.items():
        if state.pending_wifi_check_since is not None:
            asset_info = _asset_map.get(mac, {})
            pending_list.append({
                "ativo_mac": mac, "nome_ativo": asset_info.get("nome_ativo", mac),
                "pending_quarto_id": state.pending_quarto_id,
                "tempo_pendente_sec": int(now - state.pending_wifi_check_since),
                "detalhes": f"Aguardando Wi-Fi ({asset_info.get('wifi_mac', 'N/A')})"
            })
    return pending_list

def _load_maps_from_db():
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        assets = db.query(Asset).all()
        _asset_map = { a.mac_beacon: {"id": a.id, "nome_ativo": a.nome_ativo, "quarto_id": a.quarto_id, "wifi_mac": a.mac_address, "status": a.status} for a in assets }
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