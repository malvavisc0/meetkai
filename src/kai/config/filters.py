import logging

logger = logging.getLogger(__name__)

GROUP_ID_PATTERN = "@g.us"


def should_process_chat_message(
    chat_id: str,
    author: str,
    whitelist: set[str],
    blacklist: set[str],
) -> bool:
    chat_id = chat_id.strip() if isinstance(chat_id, str) else ""
    author = author.strip() if isinstance(author, str) else ""
    is_group = GROUP_ID_PATTERN in chat_id

    if not chat_id:
        logger.warning("Invalid chat ID: %r", chat_id)
        return False

    identifiers = {chat_id}
    if is_group and author:
        identifiers.add(author)

    blocked = identifiers & blacklist
    if blocked:
        logger.info("Blocked blacklisted: %s", ", ".join(sorted(blocked)))
        return False

    if not whitelist:
        return True

    allowed = identifiers & whitelist
    if allowed:
        return True

    logger.info("Blocked chat/author not in whitelist: %s", ", ".join(sorted(identifiers)))
    return False
