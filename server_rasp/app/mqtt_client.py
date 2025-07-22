# mqtt_client.py
# Módulo que gerencia a conexão com o broker MQTT e o envio de mensagens.

import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Bed
from .config import settings

# --- MUDANÇA 1: Adicionar a Fila de Publicação ---
# Esta lista em memória irá armazenar as mensagens que não puderam ser
# enviadas porque o broker estava offline.
_publish_queue = []


# Tópicos (sem alteração)
COMMAND_TOPIC = 'wyrd/baxter/esp/all/command'
BED_LIST_TOPIC = 'wyrd/baxter/beds/available'
INDIVIDUAL_COMMAND_TOPIC = 'wyrd/baxter/esp/{esp_id}/command'

# Cliente (sem alteração)
client = mqtt.Client()

def get_available_beds_macs():
    db = SessionLocal()
    try:
        available_beds = db.query(Bed.mac_beacon).filter(Bed.quarto == None).all()
        return [mac for mac, in available_beds if mac]
    finally:
        db.close()


# --- MUDANÇA 2: Modificar as Funções de Publicação Críticas ---
# Alteramos esta função para usar a fila em caso de falha.
def publish_available_beds():
    """
    Publica a lista de MACs de beacons de camas disponíveis.
    Se o cliente estiver offline, enfileira a publicação para ser enviada depois.
    """
    topic = settings.get("bed_list_topic")
    mac_list = get_available_beds_macs()
    payload = json.dumps(mac_list)

    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Enfileirando publicação da lista de camas.")
        # Adiciona um dicionário com os detalhes da mensagem à fila.
        # O 'retain=True' é importante para que seja mantido na publicação final.
        _publish_queue.append({'topic': topic, 'payload': payload, 'retain': True})
        return
    
    print(f"[MQTT] Publicando lista de beacons disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)


# Você pode aplicar a mesma lógica para outras funções críticas se as tiver.
# Por exemplo, para os vereditos, embora sejam menos críticos de reter por muito tempo.
def publish_to_esp_channel(esp_id: str, message_type: str, data: dict):
    """
    Publica uma mensagem direcionada para uma ESP específica.
    """
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio para {esp_id}.")
        # Poderíamos enfileirar aqui também, mas vereditos são sensíveis ao tempo.
        # Por enquanto, vamos manter o comportamento de descarte para estes.
        return

    topic_template = INDIVIDUAL_COMMAND_TOPIC
    if not topic_template:
        print("[MQTT] ERRO: INDIVIDUAL_COMMAND_TOPIC não encontrado nas configurações.")
        return

    individual_topic = topic_template.format(esp_id=esp_id)
    
    payload = json.dumps({
        "type": message_type,
        "data": data
    })
    
    print(f"[MQTT] Publicando no canal individual '{individual_topic}': {payload}")
    client.publish(individual_topic, payload, qos=1)

# A função de comando geral também pode ser modificada se for crítica.
def publish_command_to_all(command: dict):
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Enfileirando comando geral.")
        _publish_queue.append({'topic': settings.get("command_topic"), 'payload': json.dumps(command), 'retain': False})
        return

    topic = settings.get("command_topic")
    payload = json.dumps(command)
    print(f"[MQTT] Publicando comando GERAL no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1)


# --- MUDANÇA 3: Processar a Fila ao Reconectar ---
# Alteramos a função on_connect para esvaziar a fila.
def on_connect(client, userdata, flags, rc):
    """
    Callback executado quando a conexão com o broker é (re)estabelecida.
    """
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        # Imediatamente após conectar, publica a lista atual de camas.
        publish_available_beds()

        # --- LÓGICA DE PROCESSAMENTO DA FILA ---
        # Verifica se há mensagens pendentes que foram enfileiradas.
        if _publish_queue:
            print(f"[MQTT] Encontradas {len(_publish_queue)} mensagens na fila. Enviando agora...")
            # Itera sobre uma cópia da fila e envia cada mensagem.
            for msg in list(_publish_queue):
                client.publish(msg['topic'], msg['payload'], qos=1, retain=msg.get('retain', False))
                _publish_queue.remove(msg) # Remove a mensagem da fila original após o envio.
            print("[MQTT] Fila de mensagens pendentes processada.")

    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")


def connect_mqtt():
    # A função de conexão não precisa de alterações.
    client.on_connect = on_connect
    try:
        broker_host = settings.get("broker_host")
        broker_port = int(settings.get("broker_port", 1883))
        client.connect(broker_host, broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")