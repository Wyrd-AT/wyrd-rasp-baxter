# config.py (Versão Híbrida Final e Corrigida)
import configparser
import os
import logging
logger = logging.getLogger(__name__)

# Define o nome do arquivo de configuração
CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com TODAS as chaves necessárias
    para a arquitetura híbrida (RTLS + Baxter).
    """
    config = configparser.ConfigParser()
    
    if not os.path.exists(CONFIG_FILE):
        logger.info(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão completos.")
        
        # --- Seção [Network] (Base RTLS) ---
        config['Network'] = {
            'ip': '0.0.0.0',
            'port': '8080',
            # Chave adicionada do Baxter para o scan de presença
            'network_range_scan': '192.168.1.0/24' 
        }

        # --- Seção [MQTT] (Base RTLS) ---
        config['MQTT'] = {
            'mqtt_broker_host': '127.0.0.1',
            'mqtt_broker_port': '1883',
            'mqtt_asset_list_topic': 'wyrd/rtls/assets/available',
            'mqtt_esp_command_topic': 'wyrd/rtls/esp/all/command'
        }
        
        # --- Seção [Dispatcher] (ADICIONADA DO BAXTER) ---
        # Esta seção é a que estava em falta e causava o erro.
        config['Dispatcher'] = {
            'final_ip': '127.0.0.1', # IP do servidor final para onde os eventos são enviados
            'final_port': '9500'      # Porta do servidor final
        }

        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    # Lê o arquivo de configuração existente (ou o que acabámos de criar)
    config.read(CONFIG_FILE)
    
    # Junta todas as configurações num único dicionário (em minúsculas)
    settings = {}
    for section in config.sections():
        # configparser, por padrão, converte as chaves para minúsculas
        settings.update(config[section])
        
    return settings

# Carrega as configurações para serem importadas por outros módulos
settings = load_configuration()