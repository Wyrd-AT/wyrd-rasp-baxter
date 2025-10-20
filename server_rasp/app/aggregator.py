# aggregator.py (Versão Final com Média Móvel Exponencial)
import asyncio
import time
import json
import collections
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
    "disappearance_tolerance_cycles": 10,
    "force_penalty_on_miss": True 
}

PENALTY_RSSI = -100

class AssetState:
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}
        self.last_real_rssi = {}
        self.last_processed_avg = {} 
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "avg_rssi": -1000} 
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.disappeared_since = None
        self.weak_signal_since = None
        self.disappearance_count = 0

    def update_reading(self, esp_id, rssi, timestamp):
        """
        Atualiza a leitura de uma ESP.
        Em vez de calcular EMA, agora adiciona o RSSI a uma lista (deque) de 5 amostras.
        """
        if esp_id not in self.readings:
            self.readings[esp_id] = {
                "timestamp": timestamp,
                "samples": collections.deque(maxlen=10),
                "updated_in_last_batch": True
            }
        else:
            self.readings[esp_id]["timestamp"] = timestamp

        self.last_real_rssi[esp_id] = rssi
        self.readings[esp_id]["samples"].append(rssi)
        self.readings[esp_id]["updated_in_last_batch"] = True
        self.disappeared_since = None
        self.disappearance_count = 0

    def apply_penalties_if_needed(self):
        # --- LÓGICA DESTA FUNÇÃO SERÁ ALTERADA ---
        # Não precisamos mais da "checkbox", pois esta será a lógica padrão
        
        # Iteramos usando .items() para ter acesso ao esp_id
        for esp_id, data in self.readings.items():
            if not data.get("updated_in_last_batch", False):

                '''
                # NÃO MUDA NADA --- '''
                pass

                '''  
                # ULTIMA MÉDIA PROCESSADA --- 
                if data["samples"]:
                    current_avg = round(sum(data["samples"]) / len(data["samples"]))
                    
                    data["samples"].append(current_avg)
                '''

                ''' 
                # ÚLTIMO VALOR CONHECIDO ---   
                last_known_rssi = self.last_real_rssi.get(esp_id)
                
                # Só adicionamos se tivermos um último valor para repetir
                if last_known_rssi is not None:
                    data["samples"].append(last_known_rssi)'''
                
                '''
                # PENALIDADE FIXA (-100 dBm) ---
                data["samples"].append(PENALTY_RSSI)
                '''
            
            # Reseta o flag para o próximo ciclo
            data["updated_in_last_batch"] = False

    def get_average_rssi(self, esp_id):
        """
        (NOVA FUNÇÃO)
        Calcula e retorna a Média Móvel Simples (SMA) para uma ESP.
        """
        if esp_id not in self.readings or not self.readings[esp_id]["samples"]:
            return -1000 # Valor inválido/inexistente

        samples = self.readings[esp_id]["samples"]
        avg = sum(samples) / len(samples)
        
        # Guarda a última média calculada (substituindo last_known_ema)
        self.last_processed_avg[esp_id] = avg
        return avg

    def cleanup_old_readings(self):
        """
        Função modificada para também limpar o 'last_processed_avg'.
        """
        now = time.time()
        timeout = _config["reading_timeout_sec"]
        
        # Usamos list() para poder modificar o dicionário durante a iteração
        for esp_id, data in list(self.readings.items()):
            if now - data["timestamp"] > timeout:
                # Esta ESP não vê o ativo há 10s. Removemos seu registro.
                del self.readings[esp_id]
                if esp_id in self.last_processed_avg:
                    del self.last_processed_avg[esp_id]
        
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

            state.apply_penalties_if_needed()

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
            
            strongest_candidate = {"esp_id": None, "rssi": -1000, "avg_rssi": -1000, "quarto_id": None}
            
            for esp_id, reading_data in state.readings.items():
                if esp_id not in _esp_map: continue
                
                current_avg = state.get_average_rssi(esp_id)
                if current_avg == -1000: # Ignora se não houver amostras
                    continue

                q_id, q_rssi = _esp_map[esp_id]
                threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                
                if current_avg > threshold and current_avg > strongest_candidate["avg_rssi"]:
                    last_raw_rssi = reading_data["samples"][-1] if reading_data["samples"] else -1000
                    
                    strongest_candidate = {
                        "esp_id": esp_id, 
                        "rssi": last_raw_rssi,        # Guarda o RSSI bruto mais recente para log
                        "avg_rssi": current_avg,      # Usa a MÉDIA (SMA) para decisão
                        "quarto_id": q_id
                    }
            
            # Atualiza o sinal mais forte geral (baseado na MÉDIA)
            if state.last_processed_avg:
                top_esp, top_avg = max(state.last_processed_avg.items(), key=lambda i: i[1])
                last_raw = state.readings[top_esp]["samples"][-1] if top_esp in state.readings and state.readings[top_esp]["samples"] else -1000
                state.last_strongest_signal = {"esp_id": top_esp, "rssi": last_raw, "avg_rssi": top_avg}

            candidate_quarto_id = strongest_candidate["quarto_id"]

            candidate_quarto_id = strongest_candidate["quarto_id"]

            if quarto_id_atual is not None and not candidate_quarto_id:
                if state.weak_signal_since is None: state.weak_signal_since = time.time()
                elif (time.time() - state.weak_signal_since) * 1000 > _config["inertia_saida_ms"]:
                    logger.info(f"EVENTO OUT (SINAL FRACO): Ativo {mac} marcado para remoção do Quarto {quarto_id_atual}.")
                    changes_to_commit.append({
                        "asset_id": asset_id, "new_quarto_id": None,
                        "source_esp_id": state.last_strongest_signal['esp_id'],
                        "rssi": state.last_strongest_signal['rssi'],
                        "details": f"Sinal (SMA) permaneceu fraco por {_config['inertia_saida_ms']}ms" 
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
                    
                    leituras_ao_vivo_avg = [
                        state.get_average_rssi(e) for e in esps_no_quarto_atual 
                        if e in state.readings and state.readings[e]["samples"]
                    ]
                    
                    if leituras_ao_vivo_avg:
                        rssi_para_comparacao = max(leituras_ao_vivo_avg)
                    else:
                        avg_conhecidos = [avg for esp, avg in state.last_processed_avg.items() if esp in esps_no_quarto_atual]
                        rssi_para_comparacao = max(avg_conhecidos) if avg_conhecidos else -1000
                    
                    if strongest_candidate["avg_rssi"] < (rssi_para_comparacao + _config["conflict_margin_db"]):
                        # logger.info(f"CONFLITO: Ativo {mac}: Troca de Q{quarto_id_atual} para Q{candidate_quarto_id} NEGADA.
                        continue
                
                state.candidate_quarto_id = candidate_quarto_id
                state.candidate_since = time.time()

            if state.candidate_since and state.candidate_quarto_id is not None:
                if (time.time() - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    if state.candidate_quarto_id == candidate_quarto_id and state.candidate_quarto_id != quarto_id_atual:
                        logger.info(f"EVENTO IN: Ativo {mac} confirmado no Quarto {state.candidate_quarto_id}.")
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": state.candidate_quarto_id,
                            "source_esp_id": strongest_candidate['esp_id'],
                            "rssi": strongest_candidate['rssi'], # Logamos o RSSI bruto mais recente
                            "details": f"Localizado via {strongest_candidate['esp_id']} com RSSI Bruto {strongest_candidate['rssi']} (SMA {strongest_candidate['avg_rssi']:.1f})" 
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
        _config["force_penalty_on_miss"] = settings_from_db.get("force_penalty_on_miss", settings.get('force_penalty_on_miss', "true").lower() == "true")
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