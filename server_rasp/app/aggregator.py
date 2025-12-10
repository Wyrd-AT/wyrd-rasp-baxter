# ==============================================================================
# ARQUIVO: aggregator.py (BAXTER 2.0 - FINAL: SEM TIPOS + CORREÇÃO OCUPAÇÃO)
# ==============================================================================

import asyncio
import time
import json
import logging
import collections
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session, joinedload

from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting, ReceivedEvent
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHES GLOBAIS ---
_esp_map = {}
_asset_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()

_config = {
    "process_interval_sec": float(settings.get('process_interval_sec', 2.0)),
    "reading_timeout_sec": int(settings.get('reading_timeout_sec', 10)),
    "disappearance_tolerance_cycles": int(settings.get('disappearance_tolerance_cycles', 10)),
}
_global_settings = {
    "default_rssi_threshold": -75, 
    "inertia_entrada_ms": 3000, 
    "inertia_saida_ms": 10000,
}

# ==============================================================================
# CLASSE DE ESTADO DO ATIVO (SIMPLIFICADA)
# ==============================================================================
class AssetState:
    def __init__(self, mac, quarto_id_atual, location_status_atual):
        self.mac = mac
        if quarto_id_atual is None:
            self.state = 'LIVRE'
        else:
            self.state = location_status_atual if location_status_atual in ['PENDENTE', 'CONFIRMADO', 'ALERTA'] else 'CONFIRMADO'
        
        self.readings = {}
        
        # --- REGRAS FIXAS (BAXTER) ---
        self.algoritmo_media = 'SMA'
        self.parametro_media = 10 
        self.samples_per_esp = {}
            
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.weak_signal_since = None
        self.disappearance_count = 0

    def update_reading(self, esp_id, rssi, timestamp):
        if self.state == 'DESAPARECIDO': self.state = 'LIVRE'
        if esp_id not in self.readings: self.readings[esp_id] = {}
        self.readings[esp_id]["timestamp"] = timestamp
        self.readings[esp_id]["last_rssi"] = rssi
        
        if esp_id not in self.samples_per_esp: 
            self.samples_per_esp[esp_id] = collections.deque(maxlen=self.parametro_media)
        self.samples_per_esp[esp_id].append(rssi)
        self.disappearance_count = 0 

    def get_average_rssi(self, esp_id):
        samples = self.samples_per_esp.get(esp_id)
        if not samples: return -1000
        return sum(samples) / len(samples)

    def cleanup_old_readings(self):
        now = time.time(); timeout = _config["reading_timeout_sec"]
        active_esps = {esp_id for esp_id, data in self.readings.items() if (now - data.get("timestamp", 0)) <= timeout}
        
        self.readings = {esp_id: data for esp_id, data in self.readings.items() if esp_id in active_esps}
        self.samples_per_esp = {esp_id: samples for esp_id, samples in self.samples_per_esp.items() if esp_id in active_esps}

        if not self.readings and self.state != 'DESAPARECIDO':
            self.state = 'DESAPARECIDO'; self.disappearance_count = 0
        return bool(self.readings)

# ==============================================================================
# FUNÇÕES DE CONTROLE
# ==============================================================================

def flag_for_reload():
    _config_needs_reload.set()

def _load_maps_from_db():
    global _esp_map, _asset_map, _global_settings
    logger.info("[CACHE] Recarregando mapas (Modo Baxter: Sem Tipos)...")
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        assets = db.query(Asset).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, 
                "quarto_id": a.quarto_id, 
                "status": a.status,
                "location_status": a.location_status
            } for a in assets
        }

        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        _global_settings["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", -75))
        _global_settings["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", 3000))
        _global_settings["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", 10000))
        
        logger.info(f"[CACHE] Recarregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos.")
    finally:
        db.close()

# ==============================================================================
# MOTOR RTLS
# ==============================================================================

async def _consume_scan_data_queue():
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons_obj = payload.get("b", {})

        for mac, rssi in beacons_obj.items():
            mac = mac.lower()
            if not mac: continue
            if mac not in _asset_map: continue

            try:
                if mac not in _asset_realtime_state:
                    asset_info = _asset_map.get(mac, {})
                    quarto_id_atual = asset_info.get("quarto_id")
                    location_status_atual = asset_info.get("location_status", "LIVRE")
                    
                    # Sem regras de tipo, usa padrão direto
                    _asset_realtime_state[mac] = AssetState(
                        mac=mac, 
                        quarto_id_atual=quarto_id_atual, 
                        location_status_atual=location_status_atual
                    )
                _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time()) 
            except Exception as e:
                logger.error(f"[AGG] Erro ao processar MAC '{mac}': {e}", exc_info=True)

async def _processar_localizacoes():
    """
    Motor RTLS com correção de Race Condition (Controle de Ocupação em Tempo Real).
    """
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    
    # 1. Mapa de Ocupação Instantânea (Impede 2 camas no mesmo quarto)
    # Conta quem JÁ está no quarto segundo o banco de dados
    cycle_occupancy = collections.defaultdict(int)
    for asset in _asset_map.values():
        if asset['quarto_id']:
            cycle_occupancy[asset['quarto_id']] += 1

    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {}); asset_id = asset_info.get("id")
            if not asset_id: continue

            state.cleanup_old_readings()
            
            # --- Encontrar melhor sinal ---
            strongest_candidate = {"esp_id": None, "avg_rssi": -1000, "quarto_id": None}
            
            if state.state != 'DESAPARECIDO':
                for esp_id in state.readings:
                    if esp_id not in _esp_map: continue
                    quarto_id_candidato, rssi_min_embarcado = _esp_map[esp_id]
                    
                    # --- CORREÇÃO DE OCUPAÇÃO (Checa antes de eleger candidato) ---
                    # Regra fixa: Capacidade = 1
                    # Se o quarto já está cheio (cycle_occupancy >= 1) E este ativo NÃO é o dono da vaga...
                    if cycle_occupancy[quarto_id_candidato] >= 1:
                        if asset_info.get("quarto_id") != quarto_id_candidato:
                            # ...então ignora este sinal como candidato válido.
                            # Isso impede que ele tente roubar a vaga ou entrar junto.
                            continue 

                    current_avg = state.get_average_rssi(esp_id)
                    threshold = rssi_min_embarcado if rssi_min_embarcado is not None else _global_settings["default_rssi_threshold"]
                    
                    if current_avg > threshold and current_avg > strongest_candidate["avg_rssi"]:
                        strongest_candidate = {"esp_id": esp_id, "avg_rssi": current_avg, "quarto_id": quarto_id_candidato}

            # ========================================================
            # MÁQUINA DE ESTADOS
            # ========================================================

            if state.state == 'DESAPARECIDO':
                state.disappearance_count += 1
                if state.disappearance_count >= _config['disappearance_tolerance_cycles']:
                    if asset_info.get("quarto_id") is not None:
                        # Libera a vaga no contador instantâneo ao sair
                        cycle_occupancy[asset_info.get("quarto_id")] -= 1
                        
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE",
                            "details": "Saída por timeout."
                        })
                    del _asset_realtime_state[mac]
                continue

            elif state.state == 'LIVRE':
                candidate_quarto_id = strongest_candidate["quarto_id"]
                
                # Se mudou o candidato, reseta o timer
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = time.time() if candidate_quarto_id is not None else None

                if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _global_settings["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == candidate_quarto_id:
                        
                        # --- VERIFICAÇÃO FINAL DE SEGURANÇA ---
                        # Vai que outro ativo ocupou a vaga durante este mesmo loop?
                        if cycle_occupancy[candidate_quarto_id] >= 1:
                            state.candidate_quarto_id = None # Aborta entrada
                            continue

                        # Ocupa a vaga no contador instantâneo (bloqueia para os próximos do loop)
                        cycle_occupancy[candidate_quarto_id] += 1
                        
                        # REGRA BAXTER: Sempre vai para PENDENTE primeiro
                        change = {
                            "asset_id": asset_id, "new_quarto_id": candidate_quarto_id, 
                            "rssi": strongest_candidate.get('avg_rssi'), 
                            "location_status": "PENDENTE",
                            "details": f"Detecção BLE (Média: {strongest_candidate['avg_rssi']:.1f}dBm)."
                        }
                        state.state = 'PENDENTE'
                        changes_to_commit.append(change)
                        state.candidate_quarto_id, state.candidate_since = None, None
            
            elif state.state in ['PENDENTE', 'CONFIRMADO', 'ALERTA']:
                quarto_id_atual = asset_info.get("quarto_id")
                is_stable = (strongest_candidate.get("quarto_id") == quarto_id_atual)

                if is_stable:
                    state.weak_signal_since = None
                else:
                    if state.weak_signal_since is None:
                        state.weak_signal_since = time.time()
                    
                    elif (time.time() - state.weak_signal_since) * 1000 > _global_settings["inertia_saida_ms"]:
                        # Libera a vaga ao sair
                        cycle_occupancy[quarto_id_atual] -= 1
                        
                        esp_saida = strongest_candidate.get("esp_id", "server_exit")
                        
                        changes_to_commit.append({ 
                            "asset_id": asset_id, "new_quarto_id": None, 
                            "location_status": "LIVRE", 
                            "details": "Sinal instável ou insuficiente.", 
                            "source_esp_id": esp_saida 
                        })
                        state.state = 'LIVRE'; state.weak_signal_since = None

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
        db.commit()
    finally:
        db.close()

def clear_asset_state(mac_beacon_to_clear: str):
    if mac_beacon_to_clear in _asset_realtime_state:
        del _asset_realtime_state[mac_beacon_to_clear]
        logger.info(f"Estado de memória limpo para: {mac_beacon_to_clear}")
        return True
    return False

async def main_aggregator_loop():
    logger.info("[RTLS] Motor de localização iniciado (Modo Baxter).")
    _load_maps_from_db()
    while True:
        try:
            if _config_needs_reload.is_set():
                _load_maps_from_db(); _config_needs_reload.clear()
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico: {e}", exc_info=True)
        await asyncio.sleep(_config["process_interval_sec"])