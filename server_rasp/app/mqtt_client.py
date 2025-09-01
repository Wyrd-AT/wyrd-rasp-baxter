import paho.mqtt.client as mqtt
import json
import asyncio
import threading # <-- NOVO: Para o lock de segurança
from datetime import datetime, timezone
from .models import SessionLocal, Asset, Embarcado
from .config import settings
import logging

logger = logging.getLogger(__name__)

# --- NOVO: Fila assíncrona para o aggregator (lógica existente) ---
scan_data_queue = asyncio.Queue()

# --- NOVO: Cache em memória para o status dos ESPs e lock de segurança ---
_esp_status_cache = {}
_cache_lock = threading.Lock()

client = mqtt.Client()

def get_and_clear_status_cache():
    """
    Função segura para que a tarefa de escrita possa obter os dados do cache
    e limpá-lo para o próximo ciclo.
    """
    with _cache_lock:
        cache_copy = _esp_status_cache.copy()
        _esp_status_cache.clear()
        return cache_copy

def on_message(client, userdata, msg):
    """
    Callback super rápido que agora SÓ coloca dados na fila e no cache em memória.
    NENHUM ACESSO AO BANCO DE DADOS AQUI.
    """
    topic_parts = msg.topic.split('/')
    
    if len(topic_parts) == 5 and topic_parts[4] == "scan_data":
        esp_id = topic_parts[3]
        try:
            payload = json.loads(msg.payload)
            # 1. Coloca os dados de scan na fila do aggregator (como antes)
            scan_data_queue.put_nowait({"esp_id": esp_id, "payload": payload})

            # 2. Atualiza o cache de status com as novas informações
            with _cache_lock:
                update_data = {
                    "last_seen": datetime.now(timezone.utc)
                }
                if 'wifi_signal' in payload:
                    update_data["wifi_signal"] = payload['wifi_signal']
                
                _esp_status_cache[esp_id] = update_data

        except json.JSONDecodeError:
            logger.warning("[MQTT] JSON inválido recebido no tópico: %s", msg.topic)
        except Exception as e:
            logger.error("[MQTT] Erro ao processar mensagem de %s: %s", esp_id, e)

def on_connect(client, userdata, flags, rc):
    """Callback executado quando a conexão com o broker é (re)estabelecida."""
    if rc == 0:
        logger.info("[MQTT] Conectado com sucesso ao Broker MQTT!")
        
        # --- ALTERADO: Subscrição principal agora é para os dados de scan ---
        client.subscribe("wyrd/rtls/esp/+/scan_data", qos=0)
        logger.info("[MQTT] Subscrito ao tópico de dados 'wyrd/rtls/esp/+/scan_data'")

    else:
        logger.error(f"[MQTT] Falha ao conectar, código de retorno: {rc}")

def start_mqtt_client():
    """Inicializa e conecta o cliente MQTT."""
    logger.info("[MQTT] Iniciando cliente...")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        #client.username_pw_set("wyrd_user", "Wyrd2025")
        broker_port = int(settings.get("mqtt_broker_port"))
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")