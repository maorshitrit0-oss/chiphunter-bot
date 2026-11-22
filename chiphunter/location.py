"""Commute classification from home (train first, car second). Home = profile.json "home_city_he".

Minutes are rough door-to-door estimates by train from home, morning rush.
They are estimates, not timetable data.
"""
from __future__ import annotations

# (tier, est. door-to-door minutes from Rosh HaAyin, aliases)
#  "train" = there is a train line -> fine at any reasonable distance
#  "bus"   = no train; minutes are by bus incl. traffic. <= 90 min is fine, more is "less relevant"
#  "far"   = not relevant at all (Eilat etc.)
CITIES: dict[str, tuple[str, int | None, list[str]]] = {
    "Rosh HaAyin":   ("train", 5,   ["rosh ha'ayin", "rosh haayin", "rosh ha ayin", "ראש העין"]),
    "Petah Tikva":   ("train", 20,  ["petah tikva", "petah-tikva", "petach tikva", "petah tiqwa", "פתח תקווה", "פתח תקוה"]),
    "Bnei Brak":     ("train", 25,  ["bnei brak", "בני ברק"]),
    "Tel Aviv":      ("train", 30,  ["tel aviv", "tel-aviv", "תל אביב", "tlv"]),
    "Ramat Gan":     ("train", 30,  ["ramat gan", "רמת גן"]),
    "Givatayim":     ("train", 35,  ["givatayim", "גבעתיים"]),
    "Holon":         ("train", 45,  ["holon", "חולון"]),
    "Herzliya":      ("train", 45,  ["herzliya", "herzlia", "הרצליה"]),
    "Raanana":       ("train", 45,  ["raanana", "ra'anana", "רעננה"]),
    "Kfar Saba":     ("train", 35,  ["kfar saba", "כפר סבא"]),
    "Hod HaSharon":  ("train", 35,  ["hod hasharon", "hod ha sharon", "הוד השרון"]),
    "Lod":           ("train", 40,  ["lod", "לוד"]),
    "Rishon LeZion": ("train", 50,  ["rishon", "ראשון לציון"]),
    "Rehovot":       ("train", 50,  ["rehovot", "רחובות"]),
    "Modiin":        ("train", 55,  ["modiin", "modi'in", "מודיעין"]),
    "Netanya":       ("train", 55,  ["netanya", "נתניה"]),
    "Jerusalem":     ("train", 70,  ["jerusalem", "ירושלים"]),
    "Ashdod":        ("train", 65,  ["ashdod", "אשדוד"]),
    "Yavne":         ("train", 55,  ["yavne", "יבנה"]),
    "Ashkelon":      ("train", 80,  ["ashkelon", "אשקלון"]),
    "Kiryat Gat":    ("train", 75,  ["kiryat gat", "kiryat-gat", "קריית גת", "קרית גת"]),
    "Beer Sheva":    ("train", 95,  ["beer sheva", "be'er sheva", "beersheba", "באר שבע"]),
    "Caesarea":      ("train", 75,  ["caesarea", "קיסריה"]),
    "Hadera":        ("train", 65,  ["hadera", "חדרה"]),
    "Haifa":         ("train", 90,  ["haifa", "חיפה", "matam", "מת\"ם"]),
    "Yokneam":       ("train", 105, ["yokneam", "yoqneam", "יקנעם"]),
    "Karmiel":       ("train", 130, ["karmiel", "כרמיאל"]),
    "Akko":          ("train", 110, ["akko", "acre", "עכו"]),
    "Nahariya":      ("train", 125, ["nahariya", "נהריה"]),
    "Dimona":        ("train", 130, ["dimona", "דימונה"]),
    # no train -> bus (minutes incl. typical traffic)
    "Yehud":         ("bus",   35,  ["yehud", "יהוד", "savyon", "סביון"]),
    "Or Yehuda":     ("bus",   50,  ["or yehuda", "אור יהודה"]),
    "Airport City":  ("bus",   40,  ["airport city", "איירפורט סיטי"]),
    "Shoham":        ("bus",   45,  ["shoham", "שוהם"]),
    "Elad":          ("bus",   25,  ["elad", "אלעד"]),
    "Kfar Qasim":    ("bus",   25,  ["kafr qasim", "kfar kasem", "כפר קאסם"]),
    "Ariel":         ("bus",   40,  ["ariel", "אריאל"]),
    "Ness Ziona":    ("bus",   75,  ["ness ziona", "נס ציונה"]),
    "Kiryat Ono":    ("bus",   45,  ["kiryat ono", "קריית אונו"]),
    "Ramat HaSharon":("bus",   60,  ["ramat hasharon", "רמת השרון"]),
    "Emek Hefer":    ("bus",   85,  ["emek hefer", "עמק חפר"]),
    "Kfar Yona":     ("bus",   60,  ["kfar yona", "כפר יונה"]),
    "Omer":          ("bus",   120, ["omer", "עומר"]),
    "Migdal HaEmek": ("bus",   150, ["migdal haemek", "migdal ha'emek", "mig ha'emek", "מגדל העמק"]),
    "Nazareth":      ("bus",   150, ["nazareth", "נצרת", "nof hagalil", "נוף הגליל"]),
    "Mishor Adumim": ("bus",   100, ["mishor adumim", "מישור אדומים"]),
    "Eilat":         ("far",   None, ["eilat", "אילת"]),
    "Kiryat Shmona": ("far",   None, ["kiryat shmona", "קריית שמונה"]),
    "Arava":         ("far",   None, ["arava", "ערבה"]),
}
BUS_OK_MINUTES = 90

TIER_ICON = {"train": "🚆", "bus": "🚌", "bus_long": "🚌⚠️", "far": "❌", "unknown": "❔"}
TIER_RANK = {"train": 0, "bus": 1, "unknown": 2, "bus_long": 3, "far": 4}


def classify(location: str) -> dict:
    """Return best (most convenient) commute among all cities mentioned in the location text."""
    l = (location or "").lower()
    hits = []
    for city, (tier, mins, aliases) in CITIES.items():
        if any(a in l for a in aliases):
            hits.append({"city": city, "tier": tier, "minutes": mins})
    for h in hits:
        if h["tier"] == "bus" and (h["minutes"] or 0) > BUS_OK_MINUTES:
            h["tier"] = "bus_long"
    if not hits:
        return {"city": location or "", "tier": "unknown", "minutes": None, "icon": TIER_ICON["unknown"]}
    best = min(hits, key=lambda h: (TIER_RANK[h["tier"]], h["minutes"] or 999))
    best["icon"] = TIER_ICON[best["tier"]]
    best["all"] = [h["city"] for h in hits]
    return best


def _home() -> str:
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "profile" / "profile.json"
        return json.loads(p.read_text(encoding="utf-8")).get("home_city_he") or "הבית"
    except Exception:
        return "הבית"


def describe_he(c: dict) -> str:
    if c["tier"] == "train":
        return f"{c['icon']} {c['city']} · ברכבת, כ-{c['minutes']} דק' מ{_home()}"
    if c["tier"] == "bus":
        return f"{c['icon']} {c['city']} · אין רכבת — באוטובוס כ-{c['minutes']} דק'"
    if c["tier"] == "bus_long":
        return f"{c['icon']} {c['city']} · אין רכבת, באוטובוס מעל שעה וחצי — פחות רלוונטי"
    if c["tier"] == "far":
        return f"{c['icon']} {c['city']} · רחוק מדי"
    return f"{c['icon']} מיקום לא ידוע ({c['city'][:40]})"
