import paho.mqtt.client as mqtt
import json
import asyncio
import threading
from datetime import datetime, timezone
# Removidas importações do 'models' que não são mais usadas neste arquivo
from .config import settings
import logging

logger = logging.getLogger(__name__)

# Fila para comunicação com o Aggregator (processamento de localização)
scan_data_queue = asyncio.Queue()

# Cache em memória para status (last_seen, wifi_signal) e lock de segurança
_esp_status_cache = {}
_cache_lock = threading.Lock()

client = mqtt.Client()

def get_and_clear_status_cache():
    """
    Função segura para a tarefa agendada obter os dados do cache e limpá-lo.
    """
    with _cache_lock:
        cache_copy = _esp_status_cache.copy()
        _esp_status_cache.clear()
        return cache_copy

# --- FUNÇÃO RESTAURADA ---
def publish_command_to_esp(esp_id: str, command: dict):
    """
    Publica um comando específico para o canal individual de um embarcado.
    Essencial para os botões de 'Reiniciar' e 'Reconfigurar' da interface.
    """
    if not client.is_connected():
        logger.warning(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    command_topic = f"wyrd/rtls/esp/{esp_id}/command"
    payload = json.dumps(command)
    logger.info(f"[MQTT] Enviando comando {payload} para a ESP '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=1) # Usamos qos=1 para maior confiabilidade

def on_message(client, userdata, msg):
    """
    Callback super rápido que agora SÓ coloca dados na fila e no cache em memória.
    """
    topic_parts = msg.topic.split('/')
    
    if len(topic_parts) == 5 and topic_parts[4] == "scan_data":
        esp_id = topic_parts[3]
        try:
            payload = json.loads(msg.payload)
            # 1. Coloca os dados de scan na fila do aggregator
            scan_data_queue.put_nowait({"esp_id": esp_id, "payload": payload})

            # 2. Atualiza o cache de status com as novas informações
            with _cache_lock:
                update_data = {
                    "last_seen": datetime.now(timezone.utc)
                }
                if 'w' in payload:
                    update_data["wifi_signal"] = payload['w']
                
                _esp_status_cache[esp_id] = update_data

        except json.JSONDecodeError:
            logger.warning("[MQTT] JSON inválido recebido no tópico: %s", msg.topic)
        except Exception as e:
            logger.error("[MQTT] Erro ao processar mensagem de %s: %s", esp_id, e)

def on_connect(client, userdata, flags, rc):
    """Callback executado quando a conexão com o broker é (re)estabelecida."""
    if rc == 0:
        logger.info("[MQTT] Conectado com sucesso ao Broker MQTT!")
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
        broker_port = int(settings.get("mqtt_broker_port")) 
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60) 
        client.loop_start() 
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")