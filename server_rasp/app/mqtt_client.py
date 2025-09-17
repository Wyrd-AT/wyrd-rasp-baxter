# Conteúdo para app/mqtt_client.py
import paho.mqtt.client as mqtt
import json
import asyncio
import threading
from datetime import datetime, timezone
from .config import settings
import logging

logger = logging.getLogger(__name__)

# Fila para dados de scan (localização)
scan_data_queue = asyncio.Queue()

# Cache em memória para atualizações de status e um "lock" para segurança
_esp_status_cache = {}
_cache_lock = threading.Lock()

client = mqtt.Client()

def get_and_clear_status_cache():
    """
    Função segura para a tarefa de batch update obter os dados do cache e limpá-lo.
    """
    with _cache_lock:
        cache_copy = _esp_status_cache.copy()
        _esp_status_cache.clear()
        return cache_copy

def publish_command_to_esp(esp_id: str, command: dict):
    """
    Publica um comando para o canal individual de um embarcado.
    """
    if not client.is_connected():
        logger.warning(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    command_topic = f"wyrd/rtls/esp/{esp_id}/command"
    payload = json.dumps(command)
    logger.info(f"[MQTT] Enviando comando {payload} para a ESP '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=1)

def on_message(client, userdata, msg):
    """
    Callback super rápido que agora SÓ coloca dados na fila e no cache, sem acessar o banco.
    """
    topic_parts = msg.topic.split('/')
    
    if len(topic_parts) == 5 and topic_parts[4] == "scan_data":
        esp_id = topic_parts[3]
        try:
            payload = json.loads(msg.payload)
            # 1. Coloca dados de scan na fila do aggregator (como antes)
            scan_data_queue.put_nowait({"esp_id": esp_id, "payload": payload})

            # 2. Atualiza o cache de status com as novas informações
            with _cache_lock:
                update_data = {
                    "last_seen": datetime.now(timezone.utc)
                }
                # AQUI ESTÁ A MÁGICA: Captura o sinal de Wi-Fi do payload
                if 'w' in payload:
                    update_data["wifi_signal"] = payload['w']
                
                _esp_status_cache[esp_id] = update_data

        except json.JSONDecodeError:
            logger.warning("[MQTT] JSON inválido recebido no tópico: %s", msg.topic)
        except Exception as e:
            logger.error("[MQTT] Erro ao processar mensagem de %s: %s", esp_id, e)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("[MQTT] Conectado com sucesso ao Broker MQTT!")
        client.subscribe("wyrd/rtls/esp/+/scan_data", qos=0) 
        logger.info("[MQTT] Subscrito ao tópico de dados 'wyrd/rtls/esp/+/scan_data'")
    else:
        logger.error(f"[MQTT] Falha ao conectar, código de retorno: {rc}")

def start_mqtt_client():
    logger.info("[MQTT] Iniciando cliente...")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        broker_port = int(settings.get("mqtt_broker_port")) 
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60) 
        client.loop_start() 
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")