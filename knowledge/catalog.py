"""Read side: loads the JSON layer (database/data/<module>) + evidence from seed CSVs
and builds the symptom -> questions / red flags / diseases / links view."""
import csv
import io
import json
import threading
from collections import defaultdict
from pathlib import Path

from django.conf import settings

DATA_ROOT = Path(settings.KNOWLEDGE_DATA_ROOT)
SEED_ROOT = Path(settings.KNOWLEDGE_SEED_ROOT)

ENTITY_TYPES = ("question", "red_flag", "evidence", "disease")
STATUSES = ("approved", "not_approved", "pending")
SAFETY_ACTIONS = ("RED_FLAG", "ESCALATE", "URGENT", "EMERGENCY")

# status words used inside the files (each layer keeps its own vocabulary)
CSV_WORD = {"approved": "clinician_approved", "not_approved": "clinician_not_approved", "pending": "ai_drafted_unverified"}
JSON_WORD = {"approved": "doctor_approved", "not_approved": "doctor_not_approved", "pending": "doctor_not_reviewed"}
FROM_WORD = {"doctor_approved": "approved", "clinician_approved": "approved",
             "doctor_not_approved": "not_approved"}  # anything else reads as pending

_cache = {}
_cache_lock = threading.Lock()


def pretty(code):
    s = str(code).replace("_", " ").strip().lower()
    return s[:1].upper() + s[1:]


def read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-8-sig")), {"bom": raw.startswith(b"\xef\xbb\xbf"), "crlf": b"\r\n" in raw}


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return None, [], {"bom": False, "crlf": True}
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    rows = list(reader)
    return list(reader.fieldnames or []), rows, {"bom": raw.startswith(b"\xef\xbb\xbf"), "crlf": b"\r\n" in raw}


def normalize_question(q):
    """Some complaint question files (dermatology, endocrine) were saved with the whole
    question packed into a list inside `code`. Unpack it."""
    if isinstance(q.get("code"), list):
        v = q["code"] + [None] * 9
        return {"code": v[0], "text": v[1], "purpose": v[2], "answer_type": v[3],
                "if_yes_action": v[4], "if_no_action": v[5],
                "sequence_order": q.get("sequence_order"), "tests_symptom_code": v[6],
                "yes_weight": v[7], "no_weight": v[8],
                **({"options": q["options"]} if q.get("options") else {})}
    return q


def seed_dir(slug):
    for cand in (slug.upper(), slug.upper().replace("-", "_")):
        p = SEED_ROOT / f"{cand}_SEED"
        if p.exists():
            return p
    return None


def entry_status(entry):
    return FROM_WORD.get(entry.get("review_status") or "", None)


class Module:
    def __init__(self, slug):
        self.slug = slug
        self.dir = DATA_ROOT / slug
        self.seed = seed_dir(slug)
        self.diseases = {}      # code -> {"data","path","meta"}
        self.sets = {}          # complaint_code -> {"data","path","meta"}
        self.complaint_names = {}
        self.rf_ref = {}
        self.evidence = {}
        self.qbank_status = {}
        self.symptom_index = defaultdict(set)   # (kind, code) -> {disease codes}
        self._load()

    def _load(self):
        for p in sorted((self.dir / "diseases").glob("*.json")):
            d, meta = read_json(p)
            d["questions"] = [normalize_question(q) for q in d.get("questions", [])]
            self.diseases[d["code"]] = {"data": d, "path": p, "meta": meta}
            for c in d.get("complaints", []):
                self.symptom_index[("complaint", c["code"])].add(d["code"])
            for s in d.get("symptoms", []):
                self.symptom_index[("symptom", s["code"])].add(d["code"])
        for p in sorted((self.dir / "questions").glob("*.json")):
            d, meta = read_json(p)
            d["questions"] = [normalize_question(q) for q in d.get("questions", [])]
            self.sets[d["complaint_code"]] = {"data": d, "path": p, "meta": meta}
        for p in sorted((self.dir / "complaints").glob("*.json")):
            d, _ = read_json(p)
            self.complaint_names[d["code"]] = d.get("name") or pretty(d["code"])
        for code in self.sets:
            self.complaint_names.setdefault(code, self.sets[code]["data"].get("complaint_name") or pretty(code))
        rp = self.dir / "red_flags_reference.json"
        if rp.exists():
            for r in read_json(rp)[0].get("red_flags", []):
                self.rf_ref[r["code"]] = r
        if self.seed:
            for r in read_csv(self.seed / "disease_evidence.csv")[1]:
                if r.get("url"):
                    self.evidence[r["code"]] = r
            for r in read_csv(self.seed / "question_bank.csv")[1]:
                self.qbank_status[r["question_code"]] = r.get("source_status") or ""
            seed_codes = {r["code"] for r in read_csv(self.seed / "diseases.csv")[1]}
            self.csv_only_diseases = len(seed_codes - set(self.diseases))
        else:
            self.csv_only_diseases = 0

    # ---------- listing ----------
    def symptoms(self, q=""):
        q = q.strip().lower()
        out = []
        for (kind, code), dis in self.symptom_index.items():
            name = self.complaint_names.get(code) if kind == "complaint" else None
            name = name or pretty(code)
            if q and q not in name.lower() and q not in code.lower().replace("_", " "):
                continue
            out.append({"kind": kind, "code": code, "name": name, "diseases": len(dis),
                        "has_question_set": kind == "complaint" and code in self.sets})
        for code, s in self.sets.items():   # complaint with a question set but no linked disease
            if ("complaint", code) not in self.symptom_index:
                name = self.complaint_names[code]
                if not q or q in name.lower():
                    out.append({"kind": "complaint", "code": code, "name": name, "diseases": 0, "has_question_set": True})
        return sorted(out, key=lambda x: (x["kind"] != "complaint", x["name"].lower()))

    # ---------- entities ----------
    def _qstatus(self, code, entry, ledger):
        st = ledger.get(("question", code))
        if st:
            return st, "ledger"
        st = entry_status(entry) if entry else None
        if st:
            return st, "file"
        if FROM_WORD.get(self.qbank_status.get(code, "")) == "approved":
            return "approved", "seed_csv"
        return "pending", "default"

    def question_rows(self, dcodes, complaint, ledger):
        rows, seen = [], {}
        if complaint and complaint in self.sets:
            for q in sorted(self.sets[complaint]["data"]["questions"], key=lambda x: x.get("sequence_order") or 0):
                st, src = self._qstatus(q["code"], q, ledger)
                rows.append(self._qrow(q, st, src, "complaint_set", []))
                seen[q["code"]] = rows[-1]
        agg = {}
        for dc in dcodes:
            d = self.diseases[dc]["data"]
            for q in d["questions"]:
                a = agg.setdefault(q["code"], {"q": q, "dis": [], "seq": []})
                a["dis"].append(d["name"]); a["seq"].append(q.get("sequence_order") or 0)
        for code, a in sorted(agg.items(), key=lambda kv: (-len(kv[1]["dis"]), sum(kv[1]["seq"]) / len(kv[1]["seq"]))):
            if code in seen:
                continue
            st, src = self._qstatus(code, a["q"], ledger)
            rows.append(self._qrow(a["q"], st, src, "disease", a["dis"]))
        return rows

    @staticmethod
    def _qrow(q, st, src, origin, used_by):
        yes, no = q.get("if_yes_action") or "", q.get("if_no_action") or ""
        return {"key": q["code"], "text": q.get("text"), "purpose": q.get("purpose"),
                "answer_type": q.get("answer_type"), "options": q.get("options"),
                "if_yes": yes, "if_no": no, "seq": q.get("sequence_order"),
                "yes_weight": q.get("yes_weight"), "no_weight": q.get("no_weight"),
                "safety": any(str(yes).startswith(a) or str(no).startswith(a) for a in SAFETY_ACTIONS),
                "origin": origin, "used_by": used_by, "status": st, "status_source": src,
                "note": q.get("review_note") or ""}

    def red_flag_rows(self, dcodes, ledger):
        rows = []
        for dc in dcodes:
            d = self.diseases[dc]["data"]
            for r in d.get("red_flags", []):
                if r["code"].startswith("NONE_APPLICABLE") or not r.get("condition"):
                    continue
                key = f"{dc}|{r['code']}"
                st = ledger.get(("red_flag", key)) or entry_status(r) or "pending"
                ref = self.rf_ref.get(r["code"], {})
                rows.append({"key": key, "rf_code": r["code"], "rf_name": ref.get("name") or pretty(r["code"]),
                             "action": ref.get("action"), "disease_code": dc, "disease_name": d["name"],
                             "trigger": f"{r.get('trigger_entity_type', '')}: {r.get('trigger_entity_code', '')}",
                             "condition": r.get("condition"), "status": st,
                             "note": r.get("review_note") or ""})
        return rows

    def disease_rows(self, dcodes, kind, code, ledger):
        rows = []
        for dc in dcodes:
            d = self.diseases[dc]["data"]
            link = next((x for x in d.get("complaints" if kind == "complaint" else "symptoms", []) if x["code"] == code), {})
            st = ledger.get(("disease", dc)) or FROM_WORD.get(d.get("status") or "", "pending")
            rows.append({"key": dc, "name": d["name"], "description": d.get("description"),
                         "icd10": d.get("icd10_code"), "relationship": link.get("relationship"),
                         "frequency": link.get("frequency") or link.get("priority"),
                         "questions": len(d["questions"]), "status": st,
                         "note": d.get("review_note") or ""})
        return sorted(rows, key=lambda r: r["name"])

    def evidence_rows(self, dcodes, ledger):
        rows = []
        for dc in dcodes:
            d = self.diseases[dc]["data"]
            for ev in d.get("evidence_ids") or ([d["evidence_id"]] if d.get("evidence_id") else []):
                e = self.evidence.get(ev)
                if not e:
                    continue
                rv = (d.get("evidence_review") or {}).get(ev, {})
                st = ledger.get(("evidence", ev)) or FROM_WORD.get(rv.get("review_status", ""), None) \
                    or ("approved" if e.get("status") == "clinician_approved" else "pending")
                rows.append({"key": ev, "disease_code": dc, "disease_name": d["name"], "title": e.get("title"),
                             "url": e.get("url"), "citation": e.get("citation"), "source_name": e.get("source_name"),
                             "status": st, "note": rv.get("review_note") or ""})
        return rows

    def detail(self, kind, code, ledger):
        dcodes = sorted(self.symptom_index.get((kind, code), []))
        complaint = code if kind == "complaint" else None
        out = {"module": self.slug, "kind": kind, "code": code,
               "name": (self.complaint_names.get(code) if kind == "complaint" else None) or pretty(code),
               "questions": self.question_rows(dcodes, complaint, ledger),
               "red_flags": self.red_flag_rows(dcodes, ledger),
               "diseases": self.disease_rows(dcodes, kind, code, ledger),
               "evidence": self.evidence_rows(dcodes, ledger)}
        out["summary"] = {t: _count(out[k]) for t, k in
                          (("question", "questions"), ("red_flag", "red_flags"), ("disease", "diseases"), ("evidence", "evidence"))}
        out["notice"] = None
        if kind == "symptom" and not out["questions"]:
            out["notice"] = "No question linked to these diseases yet. Content gap."
        elif kind == "symptom":
            out["notice"] = "Symptom only (no complaint question set). Questions are collected from the linked diseases."
        return out

    # ---------- module-wide progress ----------
    def progress(self, ledger):
        allq = self.question_rows(list(self.diseases), None, ledger)
        for c in self.sets:
            allq += [r for r in self.question_rows([], c, ledger) if r["origin"] == "complaint_set"]
        counts = {"question": _count(allq),
                  "red_flag": _count(self.red_flag_rows(list(self.diseases), ledger)),
                  "disease": _count(self.disease_rows(list(self.diseases), "complaint", "", ledger)),
                  "evidence": _count(self.evidence_rows(list(self.diseases), ledger))}
        return counts

    def has_key(self, etype, key):
        if etype == "disease":
            return key in self.diseases
        if etype == "red_flag":
            dc, _, rf = key.partition("|")
            return dc in self.diseases and any(r["code"] == rf for r in self.diseases[dc]["data"].get("red_flags", []))
        if etype == "evidence":
            return any(key in (x["data"].get("evidence_ids") or []) for x in self.diseases.values())
        if etype == "question":
            return any(q["code"] == key for x in self.diseases.values() for q in x["data"]["questions"]) or \
                   any(q["code"] == key for s in self.sets.values() for q in s["data"]["questions"])
        return False


def _count(rows):
    c = {"total": len(rows), "approved": 0, "not_approved": 0, "pending": 0}
    for r in rows:
        c[r["status"]] += 1
    return c


def module_slugs():
    return sorted(p.name for p in DATA_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_"))


def get_module(slug, fresh=False):
    if slug not in module_slugs():
        return None
    with _cache_lock:
        if fresh or slug not in _cache:
            _cache[slug] = Module(slug)
        return _cache[slug]


def set_cached(slug, mod):
    with _cache_lock:
        _cache[slug] = mod


def ledger_for(slug):
    from .models import ReviewDecision
    return {(d.entity_type, d.key): d.status for d in ReviewDecision.objects.filter(module=slug)}