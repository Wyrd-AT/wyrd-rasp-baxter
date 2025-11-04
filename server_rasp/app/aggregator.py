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
    """
    Gerencia o estado completo de um único ativo em memória, incluindo sua
    máquina de estados, timers de inércia e cálculos de média de sinal.
    """
    def __init__(self, mac, tipo_de_ativo_regras, quarto_id_atual, location_status_atual):
        self.mac = mac
        # Define o estado inicial ('Livre', 'Pendente', 'Confirmado') baseado no DB
        if quarto_id_atual is None:
            self.state = 'LIVRE'
        else:
            self.state = location_status_atual if location_status_atual in ['PENDENTE', 'CONFIRMADO', 'ALERTA'] else 'CONFIRMADO'
        
        self.readings = {} # Guarda a leitura mais recente de cada ESP
        self.last_processed_avg = {} # Guarda a última média calculada (para EMA)
        
        # Carrega as regras do Tipo de Ativo
        self.algoritmo_media = tipo_de_ativo_regras.get('algoritmo_media', 'SMA')
        self.parametro_media = tipo_de_ativo_regras.get('parametro_media', 10) # 10 amostras para SMA, como no diagrama
        
        # Buffer para Média Móvel Simples (SMA), como no seu diagrama ("Calcula Média Chip BLE")
        if self.algoritmo_media == 'SMA':
            self.samples_per_esp = {}
            
        # Timers e estados para a lógica de localização
        self.candidate_quarto_id = None # O quarto para o qual o ativo é um candidato a entrar
        self.candidate_since = None     # Timer para inércia de ENTRADA
        self.weak_signal_since = None     # Timer para inércia de SAÍDA
        self.disappearance_count = 0      # Contador para o estado "Desaparecido"

    def update_reading(self, esp_id, rssi, timestamp):
        """Adiciona uma nova leitura de RSSI. Corresponde ao "Recebeu algum sinal?" = SIM."""
        if esp_id not in self.readings:
            self.readings[esp_id] = {}
            
        self.readings[esp_id]["timestamp"] = timestamp
        self.readings[esp_id]["last_rssi"] = rssi
        
        if self.algoritmo_media == 'SMA':
            if esp_id not in self.samples_per_esp:
                self.samples_per_esp[esp_id] = collections.deque(maxlen=int(self.parametro_media))
            self.samples_per_esp[esp_id].append(rssi)
            
        self.disappearance_count = 0 # Reseta o contador pois recebemos um sinal

    def get_average_rssi(self, esp_id):
        """Corresponde ao "Calcula Média Chip BLE" do diagrama."""
        current_rssi = self.readings.get(esp_id, {}).get("last_rssi", -1000)
        
        if self.algoritmo_media == 'SMA':
            samples = self.samples_per_esp.get(esp_id)
            if not samples: return -1000
            avg = sum(samples) / len(samples)
        else: # Outros algoritmos (aqui você poderia adicionar EMA, etc.)
            avg = current_rssi
            
        self.last_processed_avg[esp_id] = avg
        return avg

    def cleanup_old_readings(self):
        """Remove leituras de ESPs que não enviam sinal há muito tempo."""
        now = time.time()
        timeout = _config["reading_timeout_sec"]
        
        active_esps = {esp_id for esp_id, data in self.readings.items() if (now - data.get("timestamp", 0)) <= timeout}
        
        self.readings = {esp_id: data for esp_id, data in self.readings.items() if esp_id in active_esps}
        if self.algoritmo_media == 'SMA':
            self.samples_per_esp = {esp_id: samples for esp_id, samples in self.samples_per_esp.items() if esp_id in active_esps}

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
    """Lê mensagens da fila MQTT e inicializa/atualiza o estado em memória dos ativos."""
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons_obj = payload.get("b", {})

        for mac, rssi in beacons_obj.items():
            mac = mac.lower()
            if not mac or mac not in _asset_map:
                continue

            # Se for a primeira vez que vemos este ativo no ciclo de vida do servidor, criamos seu objeto de estado.
            if mac not in _asset_realtime_state:
                asset_info_from_cache = _asset_map.get(mac, {})
                quarto_id_atual = asset_info_from_cache.get("quarto_id")
                location_status_atual = asset_info_from_cache.get("location_status", "LIVRE")
                
                # Carrega as regras do tipo de ativo para o objeto de estado
                tipo_de_ativo_regras = {
                    'algoritmo_media': asset_info_from_cache.get('algoritmo_media', 'SMA'),
                    'parametro_media': asset_info_from_cache.get('parametro_media', 10)
                }
                
                _asset_realtime_state[mac] = AssetState(
                    mac=mac, 
                    tipo_de_ativo_regras=tipo_de_ativo_regras,
                    quarto_id_atual=quarto_id_atual,
                    location_status_atual=location_status_atual
                )
            
            _asset_realtime_state[mac].update_reading(esp_id, rssi, time.time())

async def _processar_localizacoes():
    """
    Implementa a máquina de estados do seu diagrama de fluxo.
    """
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            asset_info = _asset_map.get(mac, {})
            asset_id = asset_info.get("id")
            if not asset_id: continue

            # --- Passo 1: Limpeza e Cálculo do Sinal Mais Forte ---
            if not state.cleanup_old_readings():
                # Corresponde ao "Recebeu algum sinal?" = NÃO
                state.disappearance_count += 1
                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    if asset_info.get("quarto_id") is not None:
                        # Se o ativo estava em um quarto, agora ele sai
                        changes_to_commit.append({"asset_id": asset_id, "new_quarto_id": None, "details": "Ativo sem sinal BLE por tempo prolongado."})
                    del _asset_realtime_state[mac] # Remove o ativo da memória
                continue # Pula para o próximo ativo

            strongest_candidate = {"esp_id": None, "avg_rssi": -1000, "quarto_id": None}
            for esp_id in state.readings:
                if esp_id not in _esp_p: continue
                quarto_id_candidato, rssi_min_embarcado = _esp_map[esp_id]
                
                # Regra de negócio: um quarto só pode ter 1 ativo
                if any(other_mac != mac and other_asset.get("quarto_id") == quarto_id_candidato for other_mac, other_asset in _asset_map.items()):
                    continue
                    
                current_avg = state.get_average_rssi(esp_id)
                threshold = rssi_min_embarcado if rssi_min_embarcado is not None else _global_settings["default_rssi_threshold"]

                # Corresponde às decisões "Média > Limite"
                if current_avg > threshold and current_avg > strongest_candidate["avg_rssi"]:
                    strongest_candidate = {"esp_id": esp_id, "avg_rssi": current_avg, "quarto_id": quarto_id_candidato}

            # --- Passo 2: A MÁQUINA DE ESTADOS ---

            # Estado Atual: LIVRE
            if state.state == 'LIVRE':
                candidate_quarto_id = strongest_candidate["quarto_id"]
                
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = time.time() if candidate_quarto_id is not None else None

                # Verifica se a inércia de entrada foi cumprida
                if state.candidate_since and (time.time() - state.candidate_since) * 1000 > _global_settings["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == candidate_quarto_id: # Confirma que o candidato ainda é o mesmo
                        
                        change = {"asset_id": asset_id, "new_quarto_id": candidate_quarto_id, "rssi": strongest_candidate.get('avg_rssi'), "details": f"Detecção BLE (Média: {strongest_candidate['avg_rssi']:.1f}dBm)."}
                        
                        # AQUI ESTÁ A CHAVE: Verifica a regra "requer_confirmacao_externa"
                        if asset_info.get('requer_confirmacao_externa', False):
                            # VAI PARA O ESTADO PENDENTE (Como no seu diagrama)
                            change["location_status"] = "PENDENTE"
                            state.state = 'PENDENTE'
                        else:
                            # Se não precisasse de confirmação, iria direto para CONFIRMADO
                            change["location_status"] = "CONFIRMADO"
                            state.state = 'CONFIRMADO'
                            
                        changes_to_commit.append(change)
                        state.candidate_quarto_id, state.candidate_since = None, None

            # Estado Atual: PENDENTE, CONFIRMADO ou ALERTA (lógica de saída)
            elif state.state in ['PENDENTE', 'CONFIRMADO', 'ALERTA']:
                quarto_id_atual = asset_info.get("quarto_id")
                is_stable = (strongest_candidate.get("quarto_id") == quarto_id_atual)

                if is_stable:
                    state.weak_signal_since = None # Sinal está bom, reseta o timer de saída
                else:
                    # O sinal ficou fraco ou aponta para outro lugar. Inicia o timer.
                    if state.weak_signal_since is None:
                        state.weak_signal_since = time.time()
                    
                    # Se o timer de inércia de saída estourou...
                    elif (time.time() - state.weak_signal_since) * 1000 > _global_settings["inertia_saida_ms"]:
                        # Corresponde ao "Média > Limite" = NÃO, saindo do estado Confirmado
                        # E também ao "Cama desliga no mqtt" (a lógica HL7 fará o mesmo)
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": None, 
                            "location_status": "LIVRE", 
                            "details": "Sinal BLE no local atual tornou-se instável ou insuficiente."
                        })
                        state.state = 'LIVRE'
                        state.weak_signal_since = None # Reseta o timer

        # --- Passo 3: Comitar as Mudanças ---
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
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        assets = db.query(Asset).options(joinedload(Asset.tipo_de_ativo)).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, "nome_ativo": a.nome_ativo, "modelo": a.modelo, "quarto_id": a.quarto_id, 
                "wifi_mac": a.mac_address, "status": a.status, "location_status": a.location_status,
                "requer_confirmacao_externa": a.tipo_de_ativo.requer_confirmacao_externa if a.tipo_de_ativo else True
            } for a in assets
        }
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