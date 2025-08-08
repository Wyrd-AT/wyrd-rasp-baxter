# logging_config.py

import logging
import logging.config
import sys

logger = logging.getLogger(__name__)

def setup_logging():
    """Configura o sistema de logging para a aplicação Baxter."""
    
    LOGGING_CONFIG = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {
                'format': '%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'default',
                'stream': sys.stdout,
            },
            'file': {
                # Este handler é o responsável pela "magia" da rotação de ficheiros.
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'formatter': 'default',
                'filename': 'baxter_events.log', # Nome do ficheiro de log
                'when': 'D', # Rotaciona diariamente
                'interval': 1,
                'backupCount': 7, # Mantém os logs dos últimos 7 dias
                'encoding': 'utf-8',
            },
        },
        'root': { # O logger raiz, que apanha tudo
            'handlers': ['console', 'file'], # Envia para o terminal E para o ficheiro
            'level': 'INFO', # Nível mínimo de severidade para guardar
        },
    }

    logging.config.dictConfig(LOGGING_CONFIG)
    logger.info("INFO: Sistema de logging configurado.") # Usamos um último print para confirmar que funcionou