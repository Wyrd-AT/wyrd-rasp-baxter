# run.py
import uvicorn
import sys
import os
import logging

# Importa as configurações. Esta importação já depende do sys.path corrigido.
from app.config import settings

# --- Configuração de Logging (do seu exemplo, para um console mais limpo) ---
class No304Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (len(record.args) >= 3 and record.args[2] == 304)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "no_304": {
            "()": No304Filter,
        }
    },
    "formatters": {
        "default": { "()": "uvicorn.logging.DefaultFormatter", "fmt": "%(levelprefix)s %(message)s", "use_colors": False, },
        "access": { "()": "uvicorn.logging.AccessFormatter", "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s', "use_colors": False, },
    },
    "handlers": {
        "default": { "formatter": "default", "class": "logging.StreamHandler", "stream": "ext://sys.stderr", },
        "access": { "formatter": "access", "class": "logging.StreamHandler", "stream": "ext://sys.stdout", "filters": ["no_304"], },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

if __name__ == '__main__':
    # A importação principal da aplicação FastAPI é feita AQUI DENTRO.
    # Isso ajuda o PyInstaller a resolver as dependências corretamente.
    from app.main import app

    try:
        host_ip = settings.get('ip', '0.0.0.0')
        port_int = int(settings.get('port', 8000))
    except (ValueError, TypeError):
        host_ip = '0.0.0.0'
        port_int = 8000
        
    logger.info(f"--- WHConnect Server v0.1.1 ---")
    logger.info(f"Iniciando servidor em http://{host_ip}:{port_int}")
    logger.info("Pressione CTRL+C para encerrar.")

    uvicorn.run(
        app,
        host=host_ip,
        port=port_int,
        log_config=LOGGING_CONFIG
    )