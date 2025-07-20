# config.py
import configparser
import os

CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com TODAS as configurações padrão para o Baxter.
    """
    config = configparser.ConfigParser()
    
    if not os.path.exists(CONFIG_FILE):
        print(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão completos.")
        
        # Seção de Rede do Servidor Baxter
        config['Network'] = {
            'IP': '0.0.0.0',
            'Port': '8000',
            'Network_Range_Scan': '172.28.74.0/24'
        }
        
        # Seção do Dispatcher (para onde os eventos finais são enviados)
        config['Dispatcher'] = {
            'Final_IP': '172.28.74.51',
            'Final_Port': '9500'
        }

        # Seção de configuração do Broker MQTT
        config['MQTT'] = {
            'Broker_Host': '172.28.74.165',
            'Broker_Port': '1883',
        }

        # Seção de Parâmetros da Aplicação (que não são do DB)
        config['Application'] = {
            'History_Retention_Days': '7',
            'Event_Page_Size': '20',
            'Cleanup_Interval_Sec': '3600'
        }

        # Escreve o novo arquivo config.ini completo
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    # Lê o arquivo de configuração
    config.read(CONFIG_FILE)
    
    # Junta todas as configurações em um único dicionário para fácil acesso
    settings = {}
    for section in config.sections():
        settings.update(config[section])
        
    return settings

# Carrega as configurações para serem importadas por outros módulos
settings = load_configuration()