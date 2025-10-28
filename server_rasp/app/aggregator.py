import asyncio
import time
import json
import logging
import collections
from datetime import datetime, timezone

from sqlalchemy.orm import joinedload
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting, TipoDeAtivo, TipoDeQuarto
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- Caches e Configurações Globais ---
_esp_map = {}
_asset_map = {}
_quarto_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()

_config = {
    "process_interval_sec": float(settings.get('process_interval_sec', 2.0)),
    "reading_timeout_sec": int(settings.get('reading_timeout_sec', 10)),
    "disappearance_tolerance_cycles": int(settings.get('disappearance_tolerance_cycles', 10)),
}
_global_settings = {
    "default_rssi_threshold": -75, "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000, "inertia_saida_ms": 10000,
}

# ==============================================================================
# CLASSE DE ESTADO DO ATIVO (FINAL)
# ==============================================================================
class AssetState:
    def __init__(self, mac, tipo_de_ativo, quarto_id_atual, location_status_atual):
        self.mac = mac
        if quarto_id_atual is None: self.state = 'LIVRE'
        else: self.state = location_status_atual if location_status_atual in ['PENDENTE', 'CONFIRMADO', 'ALERTA'] else 'CONFIRMADO'
        self.readings = {}
        self.last_processed_avg = {}
        self.algoritmo_media = tipo_de_ativo.get('algoritmo_media', 'SMA')
        self.parametro_media = tipo_de_ativo.get('parametro_media', 15)
        if self.algoritmo_media == 'SMA': self.samples_per_esp = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "avg_rssi": -1000}
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.weak_signal_since = None
        self.disappearance_count = 0

    def update_reading(self, esp_id, rssi, timestamp):
        if self.state == 'DESAPARECIDO': self.state = 'LIVRE'
        if esp_id not in self.readings: self.readings[esp_id] = {}
        self.readings[esp_id]["timestamp"] = timestamp
        self.readings[esp_id]["last_rssi"] = rssi
        if self.algoritmo_media == 'SMA':
            if esp_id not in self.samples_per_esp: self.samples_per_esp[esp_id] = collections.deque(maxlen=int(self.parametro_media))
            self.samples_per_esp[esp_id].append(rssi)
        self.disappearance_count = 0

    def get_average_rssi(self, esp_id):
        current_rssi = self.readings.get(esp_id, {}).get("last_rssi")
        if current_rssi is None: return -1000
        if self.algoritmo_media == 'SMA':
            samples = self.samples_per_esp.get(esp_id)
            if not samples: return -1000
            avg = sum(samples) / len(samples)
        elif self.algoritmo_media == 'EMA':
            alpha = float(self.parametro_media)
            last_avg_for_esp = self.last_processed_avg.get(esp_id, current_rssi)
            avg = (current_rssi * alpha) + (last_avg_for_esp * (1 - alpha))
        else: avg = current_rssi
        self.last_processed_avg[esp_id] = avg
        return avg

    def cleanup_old_readings(self):
        now = time.time(); timeout = _config["reading_timeout_sec"]; active_esps = set()
        for esp_id, data in list(self.readings.items()):
            if now - data.get("timestamp", 0) <= timeout: active_esps.add(esp_id)
            else:
                del self.readings[esp_id]
                if esp_id in self.last_processed_avg: del self.last_processed_avg[esp_id]
        if self.algoritmo_media == 'SMA':
            for esp_id in list(self.samples_per_esp.keys()):
                if esp_id not in active_esps: del self.samples_per_esp[esp_id]
        if not self.readings and self.state != 'DESAPARECIDO':
            self.state = 'DESAPARECIDO'; self.disappearance_count = 0
        return bool(self.readings)

# ==============================================================================
# FUNÇÕES DE CONTROLE EXTERNO E CARREGAMENTO DE CACHE
# ==============================================================================

def flag_for_reload():
    """Sinaliza para o loop principal que os caches precisam ser recarregados do DB."""
    _config_needs_reload.set()

def _load_maps_from_db():
    """
    Função vital que carrega todas as entidades e, mais importante,
    as REGRAS DE NEGÓCIO do banco de dados para a memória RAM.
    """
    global _esp_map, _asset_map, _quarto_map, _global_settings
    logger.info("[CACHE] Recarregando todos os mapas e regras do banco de dados...")
    db = SessionLocal()
    try:
        # 1. Carrega Embarcados
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        # 2. Carrega Ativos e as regras do seu TIPO
        assets = db.query(Asset).options(joinedload(Asset.tipo_de_ativo)).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id,
                "quarto_id": a.quarto_id,
                "status": a.status,
                # REGRAS DO TIPO DE ATIVO
                "requer_confirmacao_externa": a.tipo_de_ativo.requer_confirmacao_externa if a.tipo_de_ativo else False,
                "algoritmo_media": a.tipo_de_ativo.algoritmo_media if a.tipo_de_ativo else 'SMA',
                "parametro_media": a.tipo_de_ativo.parametro_media if a.tipo_de_ativo else 15,
            } for a in assets
        }

        # 3. Carrega Quartos e as regras do seu TIPO
        quartos = db.query(Quarto).options(joinedload(Quarto.tipo_de_quarto)).all()
        _quarto_map = {
            q.id: {
                "nome": q.nome,
                # REGRAS DO TIPO DE QUARTO
                # ADICIONE ESTA LINHA
                "habilita_eventos_integracao": q.tipo_de_quarto.habilita_eventos_integracao if q.tipo_de_quarto else False,
                "capacidade_maxima": q.tipo_de_quarto.capacidade_maxima if q.tipo_de_quarto else 0,
                "permite_transicao_direta": q.tipo_de_quarto.permite_transicao_direta if q.tipo_de_quarto else True,
            } for q in quartos
        }

        # 4. Carrega configurações globais
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        _global_settings["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", -75))
        _global_settings["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", 5))
        _global_settings["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", 3000))
        _global_settings["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", 10000))
        
        logger.info(f"[CACHE] Recarregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos, {len(_quarto_map)} Quartos.")
    finally:
        db.close()

# ==============================================================================
# LÓGICA PRINCIPAL DO AGGREGATOR
# ==============================================================================

async def _consume_scan_data_queue():
    """Lê mensagens da fila MQTT e atualiza o estado em memória dos ativos."""
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons_obj = payload.get("b", {})

        for mac, rssi in beacons_obj.items():
            mac = mac.lower()
            if not mac or mac not in _asset_map:
                continue

            # Se for a primeira vez que vemos este ativo, criamos seu objeto de estado
            if mac not in _asset_realtime_state:
                
                # --- CORREÇÃO APLICADA AQUI ---
                # 1. Busca as informações completas do ativo no cache ANTES de criar o estado.
                asset_info = _asset_map.get(mac, {})
                quarto_id_atual = asset_info.get("quarto_id")
                location_status_atual = asset_info.get("location_status", "LIVRE")
                
                tipo_de_ativo_regras = {
                    'algoritmo_media': asset_info.get('algoritmo_media', 'SMA'),
                    'parametro_media': asset_info.get('parametro_media', 15)
                }

                # 2. Passa todos os quatro argumentos necessários para o construtor.
                _asset_realtime_state[mac] = AssetState(
                    mac=mac, 
                    tipo_de_ativo=tipo_de_ativo_regras,
                    quarto_id_atual=quarto_id_atual,
                    location_status_atual=location_status_atual
                )
                # --- FIM DA CORREÇÃO ---
            
            _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time())

async def _processar_localizacoes():
    """
    Versão final com máquina de estados explícita de 5 estados,
    respeitando todas as regras de negócio.
    """
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {}); asset_id = asset_info.get("id")
            if not asset_id: continue

            state.cleanup_old_readings()
            
            strongest_candidate = {"esp_id": None, "avg_rssi": -1000, "quarto_id": None}
            if state.state != 'DESAPARECIDO':
                for esp_id in state.readings:
                    if esp_id not in _esp_map: continue
                    quarto_id_candidato, rssi_min_embarcado = _esp_map[esp_id]
                    regras_quarto_candidato = _quarto_map.get(quarto_id_candidato, {})
                    capacidade = regras_quarto_candidato.get('capacidade_maxima', 0)
                    if capacidade > 0:
                        ativos_no_quarto = sum(1 for a in _asset_map.values() if a['quarto_id'] == quarto_id_candidato)
                        if ativos_no_quarto >= capacidade: continue
                    current_avg = state.get_average_rssi(esp_id)
                    threshold = rssi_min_embarcado if rssi_min_embarcado is not None else _global_settings["default_rssi_threshold"]
                    if current_avg > threshold and current_avg > strongest_candidate["avg_rssi"]:
                        strongest_candidate = {"esp_id": esp_id, "avg_rssi": current_avg, "quarto_id": quarto_id_candidato}

            # ========================================================
            # MÁQUINA DE ESTADOS EXPLÍCITA
            # ========================================================

            if state.state == 'DESAPARECIDO':
                state.disappearance_count += 1
                if state.disappearance_count >= _config['disappearance_tolerance_cycles']:
                    if asset_info.get("quarto_id") is not None:
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE",
                            "details": f"Ativo sem sinal BLE por mais de {_config['disappearance_tolerance_cycles']} ciclos."
                        })
                    del _asset_realtime_state[mac]
                continue

            elif state.state == 'LIVRE':
                candidate_quarto_id = strongest_candidate["quarto_id"]
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = time.time() if candidate_quarto_id is not None else None

                if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _global_settings["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == candidate_quarto_id:
                        change = {"asset_id": asset_id, "new_quarto_id": candidate_quarto_id, "rssi": strongest_candidate.get('avg_rssi'), "details": f"Detecção BLE (Média: {strongest_candidate['avg_rssi']:.1f}dBm)."}
                        if asset_info.get('requer_confirmacao_externa', False):
                            change["location_status"] = "PENDENTE"; state.state = 'PENDENTE'
                        else:
                            change["location_status"] = "CONFIRMADO"; state.state = 'CONFIRMADO'
                        changes_to_commit.append(change)
                        state.candidate_quarto_id, state.candidate_since = None, None
            
            elif state.state in ['PENDENTE', 'CONFIRMADO', 'ALERTA']:
                quarto_id_atual = asset_info.get("quarto_id")
                is_stable = (strongest_candidate.get("quarto_id") == quarto_id_atual)

                if is_stable:
                    # O ativo está estável no seu quarto atual. Reseta o timer de saída.
                    state.weak_signal_since = None
                else:
                    # O ativo está instável. Inicia ou continua o timer de saída.
                    if state.weak_signal_since is None:
                        state.weak_signal_since = time.time()
                    
                    # Se a inércia de saída foi atingida, decide como sair.
                    elif (time.time() - state.weak_signal_since) * 1000 > _global_settings["inertia_saida_ms"]:
                        regras_quarto_atual = _quarto_map.get(quarto_id_atual, {})
                        new_candidate_id = strongest_candidate.get("quarto_id")

                        # CENÁRIO 1: TRANSIÇÃO DIRETA (MODO MÓVEL)
                        if regras_quarto_atual.get('permite_transicao_direta', True) and new_candidate_id is not None:
                            esps_no_quarto_atual = [esp for esp, (q_id, _) in _esp_map.items() if q_id == quarto_id_atual]
                            sinais_no_quarto_atual = [state.get_average_rssi(e) for e in esps_no_quarto_atual if e in state.readings]
                            rssi_para_comparacao = max(sinais_no_quarto_atual) if sinais_no_quarto_atual else -1000
                            
                            if strongest_candidate["avg_rssi"] > (rssi_para_comparacao + _global_settings["conflict_margin_db"]):
                                # Transição direta aprovada
                                changes_to_commit.append({ "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE", "details": f"Transição direta para o local {_quarto_map.get(new_candidate_id, {}).get('nome', new_candidate_id)}." })
                                change_in = {"asset_id": asset_id, "new_quarto_id": new_candidate_id, "rssi": strongest_candidate.get('avg_rssi'), "details": f"Transição direta de {_quarto_map.get(quarto_id_atual, {}).get('nome', quarto_id_atual)}."}
                                if asset_info.get('requer_confirmacao_externa', False):
                                    change_in["location_status"] = "PENDENTE"; state.state = 'PENDENTE'
                                else:
                                    change_in["location_status"] = "CONFIRMADO"; state.state = 'CONFIRMADO'
                                changes_to_commit.append(change_in)
                                state.weak_signal_since = None # Reseta o timer após a ação
                        else:
                            # CENÁRIO 2: SAÍDA SIMPLES (MODO LEITO ou SINAL DESAPARECEU)
                            # Isso acontece se for Modo Leito OU se for Modo Móvel mas o sinal simplesmente sumiu (sem novo candidato).
                            changes_to_commit.append({ "asset_id": asset_id, "new_quarto_id": None, "location_status": "LIVRE", "details": f"Sinal no local atual tornou-se instável ou insuficiente." })
                            state.state = 'LIVRE'
                            state.weak_signal_since = None # Reseta o timer após a ação

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

async def main_aggregator_loop():
    logger.info("[RTLS] Motor de localização UNIFICADO iniciado.")
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