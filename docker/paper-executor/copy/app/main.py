from trade_common.config import settings
from trade_common.logging_config import configure_logging
from trade_common.runner import PaperExecutor


if __name__ == "__main__":
    configure_logging("paper-executor")
    PaperExecutor(settings()).run()
