"""Job sources. Each adapter: list_jobs(cfg) -> list[Job]; fetch_details(job) -> Job."""
from . import workday, amazon, apple, greenhouse, lever, comeet, smartrecruiters, google, elbit, eightfold, linkedin, ashby, workable, drushim, telegram_channels

ADAPTERS = {
    "workday": workday,
    "amazon": amazon,
    "apple": apple,
    "greenhouse": greenhouse,
    "lever": lever,
    "comeet": comeet,
    "smartrecruiters": smartrecruiters,
    "google": google,
    "elbit": elbit,
    "eightfold": eightfold,
    "linkedin": linkedin,
    "ashby": ashby,
    "workable": workable,
    "drushim": drushim,
    "telegram": telegram_channels,
}

# Search terms used where the source supports text search (EN + HE).
SEARCH_TERMS = ["student", "intern", "סטודנט"]
