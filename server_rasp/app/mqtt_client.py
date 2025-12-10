# app/mqtt_client.py (VERSÃO CORRIGIDA COM FUNÇÃO DE WI-FI)

import paho.mqtt.client as mqtt
import json
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional # Importante para a tipagem
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
    Usada pelo batch_update_esp_status no main.py.
    """
    with _cache_lock:
        cache_copy = _esp_status_cache.copy()
        _esp_status_cache.clear()
        return cache_copy

def publish_command_to_esp(esp_id: str, command: dict):
    """
    Publica um comando específico para o canal individual de um embarcado.
    Essencial para os botões de 'Reiniciar' e 'Reconfigurar' da interface.
    """
    if not client.is_connected():
        logger.warning(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    # Se esp_id for "all", envia para o tópico geral (broadcast)
    if esp_id == "all":
        command_topic = settings.get("mqtt_esp_command_topic", "wyrd/rtls/esp/all/command")
    else:
        command_topic = f"wyrd/rtls/esp/{esp_id}/command"

    payload = json.dumps(command)
    logger.info(f"[MQTT] Enviando comando {payload} para '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=1) 

def on_message(client, userdata, msg):
    """
    Callback super rápido que agora SÓ coloca dados na fila e no cache em memória.
    """
    topic_parts = msg.topic.split('/')
    
    # Tópico esperado: wyrd/rtls/esp/{ID}/scan_data
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
                # Se o payload tiver o campo 'w' (Wi-Fi Signal), salvamos
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
    
    # Configurações de conexão
    username = settings.get("mqtt_user")
    password = settings.get("mqtt_pass")
    if username and password:
        client.username_pw_set(username, password)

    try:
        broker_host = settings.get("mqtt_broker_host", "127.0.0.1")
        broker_port = int(settings.get("mqtt_broker_port", 1883))
        
        client.connect(broker_host, broker_port, 60) 
        client.loop_start() 
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")

# --- A FUNÇÃO QUE FALTAVA ---
def get_last_wifi_signal_for_esp(esp_id: str) -> Optional[int]:
    """
    Busca no cache thread-safe o último sinal de Wi-Fi conhecido para um ESP específico.
    Utilizada pelo services.py ao gerar eventos.
    """
    with _cache_lock:
        return _esp_status_cache.get(esp_id, {}).get("wifi_signal")