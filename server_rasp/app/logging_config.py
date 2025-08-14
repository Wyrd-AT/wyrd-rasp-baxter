# logging_config.py (Corrigido e com Fuso Horário)
import logging
import logging.config
import sys
from datetime import datetime

# --- NOVO: Classe para forçar o fuso horário de São Paulo ---
class SaoPauloTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # Esta função converte o tempo do log para o nosso fuso
        # Não precisa de bibliotecas externas
        from datetime import timezone, timedelta
        sao_paulo_tz = timezone(timedelta(hours=-3))
        return datetime.fromtimestamp(record.created, tz=sao_paulo_tz).strftime(datefmt)

def setup_logging():
    LOGGING_CONFIG = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {
                # --- NOVO: Usa a nossa classe de formatação customizada ---
                '()': SaoPauloTimeFormatter,
                'format': '%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
                'datefmt': '%d/%m/%Y %H:%M:%S',
            },
            'signal_formatter': {
                # --- NOVO: Usa a nossa classe e adiciona o timestamp ---
                '()': SaoPauloTimeFormatter,
                'format': '%(asctime)s - %(message)s',
                'datefmt': '%d/%m/%Y %H:%M:%S',
            },
        },
        'handlers': {
            'console': { 'class': 'logging.StreamHandler', 'formatter': 'default', 'stream': sys.stdout, },
            'file': {
                'class': 'logging.handlers.TimedRotatingFileHandler', 'formatter': 'default',
                'filename': 'rtls_events.log', 'when': 'D', 'interval': 1, 'backupCount': 7, 'encoding': 'utf-8',
            },
            'signals_handler': {
                'class': 'logging.handlers.TimedRotatingFileHandler', # <-- Alterado de FileHandler
                'formatter': 'signal_formatter',
                'filename': 'rtls_signals.log', 
                'when': 'D',            # <-- Rotação diária
                'interval': 1,          # <-- A cada 1 dia
                'backupCount': 7,       # <-- Mantém 7 ficheiros antigos
                'encoding': 'utf-8',
            },
        },
        'loggers': { 'signals': { 'handlers': ['signals_handler'], 'level': 'INFO', 'propagate': False, }, },
        'root': { 'handlers': ['console', 'file'], 'level': 'INFO', },
    }
    logging.config.dictConfig(LOGGING_CONFIG)
    logging.info("INFO: Sistema de logging configurado. Fuso horário: São Paulo.")