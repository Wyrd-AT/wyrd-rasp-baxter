# mqtt_client.py
# Módulo que gerencia a conexão com o broker MQTT e o envio de mensagens.

import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Bed
from .config import settings

# --- Seção: Definição dos Tópicos ---
# Define os nomes dos canais MQTT que serão usados. Centralizar esses nomes aqui
# (ou no config.py) é uma boa prática para evitar erros de digitação no resto do código.
COMMAND_TOPIC = 'wyrd/baxter/esp/all/command'
BED_LIST_TOPIC = 'wyrd/baxter/beds/available'
INDIVIDUAL_COMMAND_TOPIC = 'wyrd/baxter/esp/{esp_id}/command'

# --- Seção: Instância e Conexão do Cliente ---
# Cria a instância do cliente MQTT que será usada em toda a aplicação.
# As funções `on_connect` e `connect_mqtt` cuidam do ciclo de vida da conexão.
client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    """
    Função de callback: é executada automaticamente pela biblioteca paho-mqtt
    assim que a conexão com o broker é estabelecida com sucesso.
    """
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        # Assim que conecta, publica imediatamente a lista de camas disponíveis.
        # Isso garante que qualquer ESP que se conecte receba o estado mais recente.
        publish_available_beds()
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    """
    Inicia a conexão com o broker MQTT. É chamada na inicialização do servidor.
    """
    client.on_connect = on_connect
    try:
        broker_host = settings.get("broker_host")
        broker_port = int(settings.get("broker_port", 1883))
        # Estabelece a conexão com o IP e a porta do broker definidos no config.ini.
        client.connect(broker_host, broker_port, 60)
        # client.loop_start() inicia uma thread em segundo plano que mantém a conexão
        # e processa mensagens recebidas, sem bloquear o resto da aplicação.
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")


# --- Seção: Funções de Publicação ---
# Estas funções fornecem uma interface clara para enviar diferentes tipos de mensagens.

def get_available_beds_macs():
    """
    Função auxiliar que consulta o banco de dados e retorna uma lista
    de MACs de beacons de todas as camas que NÃO estão associadas a um quarto.
    """
    db = SessionLocal()
    try:
        # Query: SELECT mac_beacon FROM beds WHERE quarto IS NULL;
        available_beds = db.query(Bed.mac_beacon).filter(Bed.quarto == None).all()
        return [mac for mac, in available_beds if mac]
    finally:
        db.close()

def publish_available_beds():
    """
    Publica a lista de MACs de beacons de camas disponíveis no tópico MQTT apropriado.
    O parâmetro 'retain=True' faz com que o broker guarde a última mensagem deste tópico,
    entregando-a a qualquer novo dispositivo que se inscreva.
    """
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação.")
        return
    
    topic = settings.get("bed_list_topic")
    mac_list = get_available_beds_macs()
    payload = json.dumps(mac_list)
    print(f"[MQTT] Publicando lista de beacons disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)

def publish_command_to_all(command: dict):
    """
    Publica um comando genérico para TODAS as ESPs que estiverem inscritas no
    tópico de comando geral. Útil para broadcast (ex: "busquem novas configurações").
    """
    if not client.is_connected():
        print("[MQTT] Cliente não conectado. Abortando publicação de comando.")
        return

    topic = settings.get("command_topic")
    payload = json.dumps(command)
    print(f"[MQTT] Publicando comando GERAL no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1)

def publish_to_esp_channel(esp_id: str, message_type: str, data: dict):
    """
    Publica uma mensagem direcionada para UMA ESP específica, usando um tópico
    individual que contém o ID da ESP. Isso permite comunicação privada.
    """
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio para {esp_id}.")
        return

    topic_template = INDIVIDUAL_COMMAND_TOPIC
    if not topic_template:
        print("[MQTT] ERRO: INDIVIDUAL_COMMAND_TOPIC não encontrado nas configurações.")
        return

    # Formata o nome do tópico com o ID da ESP específica.
    individual_topic = topic_template.format(esp_id=esp_id)
    
    # Monta um payload estruturado para a ESP saber como interpretar a mensagem.
    payload = json.dumps({
        "type": message_type,
        "data": data
    })
    
    print(f"[MQTT] Publicando no canal individual '{individual_topic}': {payload}")
    client.publish(individual_topic, payload, qos=1)