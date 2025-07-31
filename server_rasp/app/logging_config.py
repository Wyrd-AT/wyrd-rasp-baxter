import logging
import logging.config
import sys
import logging
logger = logging.getLogger(__name__)

def setup_logging():
    """Configura o sistema de logging para a aplicação."""
    
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
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'formatter': 'default',
                'filename': 'rtls_events.log', # Nome do arquivo de log
                'when': 'D', # Rotaciona diariamente
                'interval': 1,
                'backupCount': 7, # Mantém os logs dos últimos 7 dias
                'encoding': 'utf-8',
            },
        },
        'loggers': {
            'app': { # Um logger específico para nossa aplicação
                'handlers': ['console', 'file'],
                'level': 'INFO',
                'propagate': False,
            },
        },
        'root': { # O logger raiz
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
    }

    logging.config.dictConfig(LOGGING_CONFIG)
    logger.info("INFO: Sistema de logging configurado.")