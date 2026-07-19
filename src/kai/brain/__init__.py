"""Brain integration: typed Morphik + crawl4ai clients and the agent tool.

This package is bot-agnostic. The cockpit's ``deployments.start()`` injects
``KAI_BRAIN_*`` env vars into the bot subprocess; ``cli.bot._start()`` reads
them after ``bot.configure()`` and, when present, builds a ``MorphikClient``
and registers the ``brain_query`` tool. No bot code touches Morphik or crawl4ai directly.
"""
