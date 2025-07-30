# config.py
import configparser
import os

# Define o nome do arquivo de configuração que ficará ao lado do .exe
CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com valores padrão.
    """
    config = configparser.ConfigParser()
    
    if not os.path.exists(CONFIG_FILE):
        print(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão.")
        # Se o config.ini não existe, cria um com valores padrão
        config['Network'] = {
            'IP': '0.0.0.0',
            'PORT': '8000'
        }
        config['MQTT'] = {
            'MQTT_BROKER_HOST': '127.0.0.1',
            'MQTT_BROKER_PORT': '1883',
            'MQTT_ASSET_LIST_TOPIC': 'wyrd/rtls/assets/available',
            'MQTT_ESP_COMMAND_TOPIC': 'wyrd/rtls/esp/all/command'
        }
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    # Lê o arquivo de configuração existente
    config.read(CONFIG_FILE)
    
    # Junta todas as configurações de todas as seções em um único dicionário
    settings = {}
    for section in config.sections():
        settings.update(config[section])
        
    return settings

# Carrega as configurações para serem importadas por outros módulos
settings = load_configuration()