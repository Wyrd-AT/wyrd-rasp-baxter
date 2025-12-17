# ==============================================================================
# ARQUIVO: app/aggregator.py (LÓGICA ANTI-ROUBO + SAÍDA POR FRAQUEZA)
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

# --- CACHES GLOBAIS ---
_esp_map = {}
_asset_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()

_handshake_confirmed_assets = set()
_handshake_invalidated_assets = set()
_last_handshake_success = {}

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
DISAPPEARANCE_TOLERANCE_CYCLES = int(settings.get('disappearance_tolerance_cycles', 10))

class AssetState:
    def __init__(self, mac, quarto_id_atual, location_status_atual, status_updated_ts=None):
        self.mac = mac
        if quarto_id_atual is None:
            self.state = 'LIVRE'
            self.pending_start_time = None
        else:
            self.state = location_status_atual if location_status_atual in ['PENDENTE', 'CONFIRMADO', 'ALERTA'] else 'CONFIRMADO'
            if self.state == 'PENDENTE':
                self.pending_start_time = status_updated_ts if status_updated_ts else time.time()
            else:
                self.pending_start_time = None
        
        self.readings = {} 
        self.samples_per_esp = {}
        self.parametro_media = 10 
        self.candidate_quarto_id = None 
        self.candidate_since = None     
        self.weak_signal_since = None   
        self.disappearance_count = 0 

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
        return sum(samples) / len(samples)

    def cleanup_old_readings(self):
        now = time.time()
        timeout = _config.get("reading_timeout_sec", 10)
        active_esps = {esp_id for esp_id, data in self.readings.items() if (now - data.get("timestamp", 0)) <= timeout}
        self.readings = {esp_id: data for esp_id, data in self.readings.items() if esp_id in active_esps}
        self.samples_per_esp = {esp_id: samples for esp_id, samples in self.samples_per_esp.items() if esp_id in active_esps}
        return bool(self.readings)

# --- FUNÇÕES DE INTERFACE ---
def clear_asset_state(mac_beacon_to_clear: str):
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        state.candidate_quarto_id = None
        state.candidate_since = None
        state.weak_signal_since = None 
        return True
    return False

def flag_for_reload():
    _config_needs_reload.set()

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
                _asset_realtime_state[mac] = AssetState(
                    mac=mac, 
                    quarto_id_atual=asset_info.get("quarto_id"),
                    location_status_atual=asset_info.get("location_status", "LIVRE"),
                    status_updated_ts=asset_info.get("updated_on_ts")
                )
            _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time())

async def _processar_localizacoes():
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {})
            asset_id = asset_info.get("id")
            nome = asset_info.get("nome_ativo", mac)
            if not asset_id: continue

            # --- REGRA 1: SE ESTÁ CONFIRMADO (CABO), O BLE NÃO MEXE ---
            # if asset_info.get("location_status") == 'CONFIRMADO':
            #     continue

            # 1. Limpeza / Timeout de Sinal (Desaparecimento completo)
            if not state.cleanup_old_readings():
                state.disappearance_count += 1
                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    if asset_info.get("quarto_id") is not None:
                        last_esp = list(state.readings.keys())[0] if state.readings else "unknown"
                        logger.info(f"[DESAPARECEU] {nome} sumiu dos sensores. Saindo do quarto...")
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE",
                            "details": "Sinal perdido (Timeout).", "source_esp_id": last_esp, "rssi": -100
                        })
                    if mac in _asset_realtime_state: del _asset_realtime_state[mac]
                continue

            # =================================================================
            # 2. ANÁLISE DE SINAIS (LOCAL vs GLOBAL)
            # =================================================================
            quarto_id_atual = asset_info.get("quarto_id")
            threshold_global = _config["default_rssi_threshold"]

            # A. Calcular RSSI no Quarto Atual (Se houver)
            rssi_local = -1000
            esp_local_id = None
            threshold_local = threshold_global

            if quarto_id_atual:
                for esp_id in state.readings:
                     # Se este ESP pertence ao quarto atual
                     if esp_id in _esp_map and _esp_map[esp_id][0] == quarto_id_atual:
                         avg = state.get_average_rssi(esp_id)
                         if avg > rssi_local:
                             rssi_local = avg
                             esp_local_id = esp_id
                
                # Pega threshold específico do ESP local, se houver
                if esp_local_id:
                    _, t_ind = _esp_map[esp_local_id]
                    if t_ind is not None: threshold_local = t_ind

            # B. Calcular RSSI Global (Melhor de Todos)
            rssi_global = -1000
            esp_global_id = None
            quarto_global_id = None
            threshold_global_winner = threshold_global

            for esp_id in state.readings:
                if esp_id not in _esp_map: continue
                qid, t_ind = _esp_map[esp_id]

                # Ignora quartos ocupados (exceto o meu próprio)
                quarto_ocupado = False
                for omac, oinfo in _asset_map.items():
                    if omac != mac and oinfo.get("quarto_id") == qid and qid is not None:
                         if qid != quarto_id_atual: 
                             quarto_ocupado = True; break
                if quarto_ocupado: continue

                avg = state.get_average_rssi(esp_id)
                if avg > rssi_global:
                    rssi_global = avg
                    esp_global_id = esp_id
                    quarto_global_id = qid
                    if t_ind is not None: threshold_global_winner = t_ind

            # =================================================================
            # 3. LÓGICA DE DECISÃO (ANTI-ROUBO + PERMISSÃO DE SAÍDA)
            # =================================================================
            
            # Vencedor Final
            winner_esp = esp_global_id
            winner_rssi = rssi_global
            winner_quarto = quarto_global_id
            threshold_to_compare = threshold_global_winner

            # REGRA ANTI-ROUBO:
            # Se tenho quarto E meu sinal nele é BOM (>= threshold), eu ignoro o global.
            # "Ninguém me tira daqui se estou forte."
            protected = False
            if quarto_id_atual and rssi_local >= threshold_local:
                protected = True
                winner_esp = esp_local_id
                winner_rssi = rssi_local
                winner_quarto = quarto_id_atual
                threshold_to_compare = threshold_local

            # REGRA DE SAÍDA:
            # Se não estou protegido (sinal local fraco ou ausente), o vencedor é o Global.
            # Se o vencedor Global for EU MESMO (local) mas fraco, a máquina de estados abaixo vai tratar como instável.

            # =================================================================
            # 4. MÁQUINA DE ESTADOS
            # =================================================================
            
            # --- TIMEOUT DE PENDENTE ---
            # if state.state == 'PENDENTE' and state.pending_start_time:
            #     if (time.time() - state.pending_start_time) > _config.get("pending_timeout_sec", 1800):
            #         changes_to_commit.append({
            #             "asset_id": asset_id, "new_quarto_id": asset_info.get("quarto_id"),
            #             "location_status": "ALERTA", "details": "Tempo limite excedido.",
            #             "source_esp_id": winner_esp, "rssi": int(winner_rssi)
            #         })
            #         state.state = 'ALERTA'; state.pending_start_time = None

            # --- ENTRADA (LIVRE -> PENDENTE) ---
            if state.state == 'LIVRE':
                # Só entra se o sinal for BOM (acima do threshold do vencedor)
                cand_qid = winner_quarto if winner_rssi > threshold_to_compare else None
                
                if cand_qid != state.candidate_quarto_id:
                    state.candidate_quarto_id = cand_qid
                    state.candidate_since = time.time() if cand_qid else None

                if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == cand_qid and cand_qid is not None:
                        logger.info(f"[ENTRADA] {nome} -> Quarto {cand_qid} (Sinal: {winner_rssi:.1f})")
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": cand_qid, "location_status": "PENDENTE", 
                            "details": "Entrada.", "source_esp_id": winner_esp, "rssi": int(winner_rssi)
                        })
                        state.state = 'PENDENTE'; state.pending_start_time = time.time()
                        state.candidate_quarto_id, state.candidate_since = None, None

            # --- SAÍDA/MANUTENÇÃO (DENTRO -> LIVRE ou PERMANECE) ---
            elif state.state in ['PENDENTE', 'CONFIRMADO', 'ALERTA']:
                # Estabilidade:
                # 1. O vencedor deve ser o meu quarto atual.
                # 2. O sinal deve estar ACIMA do limite.
                is_stable = (winner_quarto == quarto_id_atual) and (winner_rssi >= threshold_to_compare)

                if is_stable:
                    if state.weak_signal_since is not None:
                        logger.info(f"[SINAL] {nome} estabilizou em {quarto_id_atual} ({winner_rssi:.1f} >= {threshold_to_compare}).")
                        state.weak_signal_since = None
                else:
                    # Instável: Sinal fraco ou o vencedor é outro quarto (mas só muda se sair primeiro)
                    if state.weak_signal_since is None: 
                        state.weak_signal_since = time.time()
                        # Log detalhado para entender a decisão
                        motivo = "Sinal Fraco" if (winner_quarto == quarto_id_atual) else "Melhor em Outro"
                        logger.info(f"[INSTAVEL] {nome}. Local: {rssi_local:.1f} (Lim: {threshold_local}). Global: {winner_rssi:.1f} em {winner_quarto}. Motivo: {motivo}")
                    
                    elapsed = (time.time() - state.weak_signal_since) * 1000
                    if elapsed > _config["inertia_saida_ms"]:
                        logger.info(f"[SAIDA] {nome} saindo de {quarto_id_atual} após {elapsed/1000:.1f}s instável.")
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE", 
                            "details": "Sinal fraco ou ausente.", "source_esp_id": winner_esp, "rssi": int(winner_rssi)
                        })
                        state.state = 'LIVRE'; state.pending_start_time = None; state.weak_signal_since = None

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
            db.commit()
    finally:
        db.close()

def confirm_asset_by_handshake(mac_beacon: str): pass 
def invalidate_asset_by_handshake(mac_beacon: str): pass

def _load_maps_from_db():
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        assets = db.query(Asset).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, "nome_ativo": a.nome_ativo, "quarto_id": a.quarto_id, 
                "location_status": a.location_status,
                "updated_on_ts": a.location_status_updated_on.timestamp() if a.location_status_updated_on else None,
            } for a in assets
        }
        settings_list = db.query(GlobalSetting).all()
        s_dict = {s.key: s.value for s in settings_list}
        if "rssi_threshold" in s_dict: _config["default_rssi_threshold"] = int(s_dict["rssi_threshold"])
        if "inercia_entrada" in s_dict: _config["inertia_entrada_ms"] = int(s_dict["inercia_entrada"])
        if "inercia_saida" in s_dict: _config["inertia_saida_ms"] = int(s_dict["inercia_saida"])
        _config["pending_timeout_sec"] = int(settings.get('pending_timeout_sec', 1800))
    except Exception as e:
        logger.error(f"[RTLS] Erro maps: {e}")
    finally:
        db.close()

async def main_aggregator_loop():
    _load_maps_from_db()
    while True:
        try:
            if _config_needs_reload.is_set(): _load_maps_from_db(); _config_needs_reload.clear()
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e: logger.error(f"[RTLS] Erro loop: {e}")
        await asyncio.sleep(_config["process_interval_sec"])