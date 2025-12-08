import logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO, 
    encoding="utf-8"
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

log_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler = logging.FileHandler("/data/INFO.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(log_format)

file_handler2 = logging.FileHandler("/data/DEBUG.log", encoding="utf-8")
file_handler2.setLevel(logging.DEBUG)
file_handler2.setFormatter(log_format)

file_handler3 = logging.FileHandler("/data/ERROR.log", encoding="utf-8")
file_handler3.setLevel(logging.ERROR)
file_handler3.setFormatter(log_format)

logger.addHandler(file_handler)
logger.addHandler(file_handler2)
logger.addHandler(file_handler3)
