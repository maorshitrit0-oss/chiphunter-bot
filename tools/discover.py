"""Find which companies expose their jobs through a public ATS API, and how many Israel student roles they have.
Run in GitHub Actions (open network):  python -m chiphunter.pipeline discover
Prints JSON lines of hits that can be added to data/companies.json.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")
from chiphunter.http import _req, _KW  # noqa: E402

STUDENT = re.compile(r"student|intern|סטודנט", re.I)
ISRAEL = re.compile(r"israel|tel aviv|haifa|herzliya|petah|jerusalem|yokneam|netanya|ra'?anana|kfar saba|rehovot|"
                    r"be'?er sheva|hod hasharon|caesarea|ramat gan|kiryat|airport city|rosh ha|modiin|omer|ashdod|"
                    r"migdal|lod|holon|yehud|or yehuda|\bisr\b|\bil\b", re.I)

# (company, tenant guesses) for Workday
WORKDAY = {
    "Altera": ["altera"], "Broadcom": ["broadcom"], "Cadence": ["cadence"], "Synopsys": ["synopsys"], "KLA": ["kla"],
    "Analog Devices": ["analogdevices"], "Western Digital": ["wdc"], "SanDisk": ["sandisk"], "Microchip": ["microchip"],
    "NXP": ["nxp"], "Motorola Solutions": ["motorolasolutions"], "Dell": ["dell"], "Cisco": ["cisco"],
    "Mobileye": ["mobileye"], "SolarEdge": ["solaredge"], "Tower Semiconductor": ["towersemi", "tower"],
    "Nokia": ["nokia"], "Siemens": ["siemens"], "Keysight": ["keysight"], "Teradyne": ["teradyne"],
    "Infineon": ["infineon"], "onsemi": ["onsemi"], "Micron": ["micron"], "AMD": ["amd"], "HP": ["hp"],
    "Texas Instruments": ["ti", "texasinstruments"], "Medtronic": ["medtronic"], "Philips": ["philips"],
    "GE HealthCare": ["gehc"], "Ciena": ["ciena"], "Arm": ["arm"], "Lam Research": ["lamresearch"],
    "Ceragon": ["ceragon"], "Nova": ["novami", "nova"], "Camtek": ["camtek"], "Orbotech": ["kla"],
    "Elbit Systems of America": ["elbitamerica"], "Rafael": ["rafael"], "IAI": ["iai"],
}
SITES = ["External", "Careers", "External_Career", "External_Careers", "Search", "{n}", "{n}Careers",
         "{n}_Careers", "{n}External", "jobs", "careers", "en-US"]
SHARDS = ["wd1", "wd3", "wd5", "wd12", "wd103", "wd108"]

# names to try on Greenhouse / Lever / SmartRecruiters / Ashby / Workable
NAMES = """hailo drivenets innoviz valens solaredge ceva arbe arberobotics vayyar wiliot nextsilicon neuroblade speedata
pliops weka xsightlabs teramount quantummachines classiq proteantecs retym enfabrica autotalks mobileye towersemi
ramonspace ramon-space tufin unitronics orcam lumus nanodimension stratasys kornit camtek nova
eyeonid sensata sckipio celeno gamida mantis alphatau regulus d-fend smartshooter xtend elbit rafael iai
nvidia marvell intel apple amazon google qualcomm broadcom cadence synopsys kla amat applied
infineon nxp ti analog microchip onsemi micron amd arm samsung sk-hynix sandisk westerndigital
siemens keysight teradyne ciena nokia ericsson cisco juniper arista dell hp hpe ibm microsoft meta
medtronic philips ge-healthcare biosense insightec itamar sanmina flex jabil celestica
ondas plasan bird aero-sentinel aeronautics uvision bluebird heven skydio percepto airobotics
xjet zeekit hexagon optimal-plus optimalplus tower tsg rada controp elta vishay kulicke kns
satixfy gilat spacecom ramon nsl nsl-comm siklu ceragon radwin audiocodes silicom allot
""".split()


def get(url, **kw):
    try:
        return _req.get(url, timeout=15, **_KW, **kw)
    except Exception:
        return None


def post(url, **kw):
    try:
        return _req.post(url, timeout=15, **_KW, **kw)
    except Exception:
        return None


def try_workday(company, tenant, shard, site):
    host = f"{tenant}.{shard}.myworkdayjobs.com"
    r = post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
             json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "student Israel"})
    if not r or r.status_code != 200:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    posts = d.get("jobPostings", [])
    il = [p["title"] for p in posts if ISRAEL.search(p.get("locationsText", "") + p.get("externalPath", ""))]
    return {"company": company, "type": "workday", "host": host, "site": site, "total": d.get("total"),
            "israel_hits": len(il), "sample": il[:4]}


def workday_jobs():
    tasks = []
    for company, tenants in WORKDAY.items():
        n = re.sub(r"[^A-Za-z]", "", company)
        for t in tenants:
            for shard in SHARDS:
                for site in SITES:
                    tasks.append((company, t, shard, site.format(n=n)))
    found = {}
    with ThreadPoolExecutor(24) as ex:
        for res in ex.map(lambda a: try_workday(*a), tasks):
            if res and res["company"] not in found:
                found[res["company"]] = res
    return list(found.values())


def ats(name):
    out = []
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs")
    if r is not None and r.status_code == 200:
        jobs = r.json().get("jobs", [])
        il = [j["title"] for j in jobs if ISRAEL.search((j.get("location") or {}).get("name", ""))]
        if il:
            out.append({"type": "greenhouse", "token": name, "israel": len(il), "students": [t for t in il if STUDENT.search(t)][:5]})
    r = get(f"https://api.lever.co/v0/postings/{name}?mode=json")
    if r is not None and r.status_code == 200 and isinstance(r.json(), list):
        il = [j["text"] for j in r.json() if ISRAEL.search(json.dumps(j.get("categories", {})))]
        if il:
            out.append({"type": "lever", "token": name, "israel": len(il), "students": [t for t in il if STUDENT.search(t)][:5]})
    r = get(f"https://api.smartrecruiters.com/v1/companies/{name}/postings?country=il&limit=100")
    if r is not None and r.status_code == 200 and r.json().get("totalFound", 0):
        il = [j["name"] for j in r.json().get("content", [])]
        out.append({"type": "smartrecruiters", "token": name, "israel": len(il), "students": [t for t in il if STUDENT.search(t)][:5]})
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    if r is not None and r.status_code == 200:
        il = [j["title"] for j in r.json().get("jobs", []) if ISRAEL.search(j.get("location", "") + json.dumps(j.get("secondaryLocations", [])))]
        if il:
            out.append({"type": "ashby", "token": name, "israel": len(il), "students": [t for t in il if STUDENT.search(t)][:5]})
    r = get(f"https://apply.workable.com/api/v1/widget/accounts/{name}")
    if r is not None and r.status_code == 200:
        il = [j["title"] for j in r.json().get("jobs", []) if (j.get("country") or "").lower() in ("israel", "il")]
        if il:
            out.append({"type": "workable", "token": name, "israel": len(il), "students": [t for t in il if STUDENT.search(t)][:5]})
    return out


def main():
    for hit in workday_jobs():
        print("HIT", json.dumps(hit, ensure_ascii=False))
    with ThreadPoolExecutor(16) as ex:
        for res in ex.map(ats, NAMES):
            for hit in res:
                print("HIT", json.dumps(hit, ensure_ascii=False))
    print("DONE")


if __name__ == "__main__":
    main()
