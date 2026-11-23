"""Keep OAuth callback parameters out of Uvicorn's access log."""

import logging
from urllib.parse import unquote


class OAuthCallbackQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access formatter unpacks these five arguments itself, so
        # sanitize the target argument rather than rendering record.msg early.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            if isinstance(target, str):
                path, separator, _query = target.partition("?")
                if separator and unquote(path) == "/oauth/google/callback/":
                    record.args = (client, method, path, version, status)
        return True


def install_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthCallbackQueryFilter) for item in logger.filters):
        logger.addFilter(OAuthCallbackQueryFilter())
