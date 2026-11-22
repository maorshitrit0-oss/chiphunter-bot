"""Shared data model: every source returns Job objects."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib


@dataclass
class Job:
    company: str
    title: str
    url: str                      # direct link to the specific posting (never a homepage)
    source: str                   # e.g. "workday", "amazon", "manual"
    location: str = ""
    req_id: str = ""
    posted: str = ""
    description: str = ""         # plain text; may be empty until fetch_details()
    apply_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        key = f"{self.company}|{self.req_id or self.url}".lower()
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = {k: v for k, v in d.items() if k != "uid"}
        return cls(**d)
