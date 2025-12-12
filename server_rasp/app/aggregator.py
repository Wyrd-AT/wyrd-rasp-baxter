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
import collections
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
    def __init__(self, mac, quarto_id_atual, location_status_atual, status_updated_ts=None):
        self.mac = mac
        
        # Estado inicial
        if quarto_id_atual is None:
            self.state = 'LIVRE'
            self.pending_start_time = None
        else:
            self.state = location_status_atual if location_status_atual in ['PENDENTE', 'CONFIRMADO', 'ALERTA'] else 'CONFIRMADO'
            
            # Se já nasceu PENDENTE, define o início baseado no banco (ou agora, se for nulo)
            if self.state == 'PENDENTE':
                self.pending_start_time = status_updated_ts if status_updated_ts else time.time()
            else:
                self.pending_start_time = None
        
        self.readings = {} 
        self.last_processed_avg = {} 
        self.samples_per_esp = {}
        
        self.algoritmo_media = 'SMA'
        self.parametro_media = 10 
            
        self.candidate_quarto_id = None 
        self.candidate_since = None     
        self.weak_signal_since = None   
        self.disappearance_count = 0 

    # ... (Mantenha os métodos update_reading, get_average_rssi e cleanup_old_readings iguais ao anterior) ...
    def update_reading(self, esp_id, rssi, timestamp):
        if esp_id not in self.readings: self.readings[esp_id] = {}
        self.readings[esp_id]["timestamp"] = timestamp
        self.readings[esp_id]["last_rssi"] = rssi
        if esp_id not in self.samples_per_esp: self.samples_per_esp[esp_id] = collections.deque(maxlen=int(self.parametro_media))
        self.samples_per_esp[esp_id].append(rssi)
        self.disappearance_count = 0 

    def get_average_rssi(self, esp_id):
        samples = self.samples_per_esp.get(esp_id)
        if not samples: return -1000.0
        avg = sum(samples) / len(samples)
        self.last_processed_avg[esp_id] = avg
        return avg

    def cleanup_old_readings(self):
        now = time.time()
        timeout = _config.get("reading_timeout_sec", 10)
        active_esps = {esp_id for esp_id, data in self.readings.items() if (now - data.get("timestamp", 0)) <= timeout}
        self.readings = {esp_id: data for esp_id, data in self.readings.items() if esp_id in active_esps}
        self.samples_per_esp = {esp_id: samples for esp_id, samples in self.samples_per_esp.items() if esp_id in active_esps}
        return bool(self.readings)

# --- FUNÇÕES DE INTERFACE E CONTROLE ---
def update_wifi_presence_cache(latest_cache: dict):
    global _wifi_presence_cache
    _wifi_presence_cache = latest_cache

def clear_asset_state(mac_beacon_to_clear: str):
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        state.candidate_quarto_id = None
        state.candidate_since = None
        state.weak_signal_since = None # Se tiver essa propriedade no objeto state

        _handshake_confirmed_assets.discard(mac_beacon_to_clear)
        _handshake_invalidated_assets.discard(mac_beacon_to_clear)
        _last_handshake_success.pop(mac_beacon_to_clear, None)
        
        logger.info(f"Estado de memória para o ativo {mac_beacon_to_clear} foi limpo.")
        return True
    return False

def update_asset_cache(mac_beacon: str, new_quarto_id: int | None):
    if mac_beacon in _asset_map: _asset_map[mac_beacon]["quarto_id"] = new_quarto_id

def flag_for_reload():
    _config_needs_reload.set()

# --- FUNÇÕES INTERNAS DO MOTOR RTLS ---
async def _consume_scan_data_queue():
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons_obj = payload.get("b", {})

        for mac, rssi in beacons_obj.items():
            mac = mac.lower()
            if not mac or mac not in _asset_map: continue

            if mac not in _asset_realtime_state:
                asset_info = _asset_map.get(mac, {})
                
                # Cria passando o timestamp da última atualização
                _asset_realtime_state[mac] = AssetState(
                    mac=mac, 
                    quarto_id_atual=asset_info.get("quarto_id"),
                    location_status_atual=asset_info.get("location_status", "LIVRE"),
                    status_updated_ts=asset_info.get("updated_on_ts")
                )
            
            _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time())

# ARQUIVO: app/aggregator.py

async def _processar_localizacoes():
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {})
            asset_id = asset_info.get("id")
            if not asset_id: continue

            # 1. Limpeza / Timeout de Sinal
            if not state.cleanup_old_readings():
                state.disappearance_count += 1
                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    if asset_info.get("quarto_id") is not None:
                        # CORREÇÃO 1: Tenta pegar o último ESP conhecido para registrar o log de perda
                        last_esp = list(state.readings.keys())[0] if state.readings else "unknown"
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": None, 
                            "location_status": "LIVRE",
                            "details": "Sinal perdido (Timeout).",
                            "source_esp_id": last_esp, # Ajuda a pegar o WiFi do local onde sumiu
                            "rssi": -100
                        })
                    if mac in _asset_realtime_state: del _asset_realtime_state[mac]
                continue

            # 2. Cálculo do melhor sinal
            strongest = {"esp_id": None, "avg_rssi": -1000, "quarto_id": None}
            
            for esp_id in state.readings:
                if esp_id not in _esp_map: continue
                qid, thresh_individual = _esp_map[esp_id]
                
                # Anti-roubo: Só considera se o quarto estiver vazio ou for o da própria cama
                quarto_ocupado = False
                for omac, oinfo in _asset_map.items():
                    if omac != mac and oinfo.get("quarto_id") == qid:
                        quarto_ocupado = True; break
                if quarto_ocupado: continue
                
                avg = state.get_average_rssi(esp_id)
                # Seleciona o melhor, independente de threshold (para saber onde está, mesmo que fraco)
                if avg > strongest["avg_rssi"]:
                    strongest = {"esp_id": esp_id, "avg_rssi": avg, "quarto_id": qid}

            # Define Threshold efetivo para decisões
            threshold_efetivo = _config["default_rssi_threshold"]
            if strongest["esp_id"] and strongest["esp_id"] in _esp_map:
                _, t_ind = _esp_map[strongest["esp_id"]]
                if t_ind is not None: threshold_efetivo = t_ind

            # 3. MÁQUINA DE ESTADOS

            # --- TIMEOUT DE PENDENTE ---
            if state.state == 'PENDENTE' and state.pending_start_time:
                if (time.time() - state.pending_start_time) > _config.get("pending_timeout_sec", 1800):
                    changes_to_commit.append({
                        "asset_id": asset_id,
                        "new_quarto_id": asset_info.get("quarto_id"),
                        "location_status": "ALERTA", 
                        "details": "Tempo limite de Pendência excedido.",
                        # Mantém dados do ESP atual para o log
                        "source_esp_id": strongest["esp_id"],
                        "rssi": int(strongest["avg_rssi"])
                    })
                    state.state = 'ALERTA'
                    state.pending_start_time = None

            # --- ENTRADA (LIVRE) ---
            if state.state == 'LIVRE':
                # Só considera candidato se o sinal for BOM (acima do threshold)
                cand_qid = strongest["quarto_id"] if strongest["avg_rssi"] > threshold_efetivo else None
                
                if cand_qid != state.candidate_quarto_id:
                    state.candidate_quarto_id = cand_qid
                    state.candidate_since = time.time() if cand_qid else None

                if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == cand_qid and cand_qid is not None:
                        # CORREÇÃO 2: Passa RSSI e ESP ID na entrada
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": cand_qid, 
                            "location_status": "PENDENTE", 
                            "details": "Entrada no quarto (Aguardando Cama ligar).",
                            "source_esp_id": strongest["esp_id"], # CRÍTICO: Pega Wi-Fi deste ESP
                            "rssi": int(strongest["avg_rssi"])    # CRÍTICO: Grava média BLE
                        })
                        state.state = 'PENDENTE'
                        state.pending_start_time = time.time()
                        state.candidate_quarto_id, state.candidate_since = None, None

            # --- SAÍDA (DENTRO) ---
            elif state.state in ['PENDENTE', 'CONFIRMADO', 'ALERTA']:
                quarto_id_atual = asset_info.get("quarto_id")
                
                # --- CORREÇÃO CRÍTICA AQUI ---
                # Estável = (É o quarto certo) E (O sinal está ACIMA do threshold)
                # Se o sinal cair abaixo do threshold, mesmo sendo o "vencedor", ele vira instável.
                is_stable = (strongest.get("quarto_id") == quarto_id_atual) and (strongest["avg_rssi"] >= threshold_efetivo)

                if is_stable:
                    state.weak_signal_since = None
                else:
                    # Inicia contagem de saída (seja por mudar de quarto ou por sinal fraco)
                    if state.weak_signal_since is None: state.weak_signal_since = time.time()
                    
                    if (time.time() - state.weak_signal_since) * 1000 > _config["inertia_saida_ms"]:
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": None, 
                            "location_status": "LIVRE", 
                            "details": "Saída confirmada (Sinal fraco ou ausente).",
                            "source_esp_id": strongest["esp_id"], 
                            "rssi": int(strongest["avg_rssi"])
                        })
                        state.state = 'LIVRE'
                        state.pending_start_time = None
                        state.weak_signal_since = None

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
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
        # 1. Carrega ESPs
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        # 2. Carrega Ativos 
        # IMPORTANTE: Carregamos 'location_status_updated_on' para calcular o tempo de pendência
        assets = db.query(Asset).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, 
                "nome_ativo": a.nome_ativo, 
                "quarto_id": a.quarto_id, 
                "location_status": a.location_status,
                # Convertemos para timestamp UNIX se existir, senão None
                "updated_on_ts": a.location_status_updated_on.timestamp() if a.location_status_updated_on else None,
                "requer_confirmacao_externa": True
            } for a in assets
        }
        
        # 3. Carrega Settings
        settings_list = db.query(GlobalSetting).all()
        s_dict = {s.key: s.value for s in settings_list}
        
        if "rssi_threshold" in s_dict: _config["default_rssi_threshold"] = int(s_dict["rssi_threshold"])
        if "inercia_entrada" in s_dict: _config["inertia_entrada_ms"] = int(s_dict["inercia_entrada"])
        if "inercia_saida" in s_dict: _config["inertia_saida_ms"] = int(s_dict["inercia_saida"])

        # Configuração do Tempo Limite de Pendente (Padrão: 30 minutos = 1800 segundos)
        _config["pending_timeout_sec"] = int(s_dict.get("pending_timeout", 300))

        logger.info(f"[RTLS] Configuração -> Pendente Timeout: {_config['pending_timeout_sec']}s")
        
    except Exception as e:
        logger.error(f"[RTLS] Erro ao carregar mapas: {e}", exc_info=True)
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