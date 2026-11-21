"""Write side: one decision -> JSON files, seed CSVs and vector chunk metadata.
Every file is backed up once (first touch) and replaced atomically."""
import csv
import io
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import catalog
from .catalog import CSV_WORD, JSON_WORD, DATA_ROOT, SEED_ROOT

LOCK = threading.Lock()
BACKUP_ROOT = DATA_ROOT.parent / "_review_backups"


def _rel(p):
    return str(Path(p).relative_to(DATA_ROOT.parent)).replace("\\", "/")


def _backup(path):
    path = Path(path)
    dest = BACKUP_ROOT / path.relative_to(DATA_ROOT.parent)
    if path.exists() and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _atomic_write(path, data: bytes):
    """Returns False (and touches nothing) when the file would not change."""
    path = Path(path)
    if path.exists() and path.read_bytes() == data:
        return False
    _backup(path)
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def write_json(path, data, meta):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if meta.get("crlf"):
        text = text.replace("\n", "\r\n")
    raw = text.encode("utf-8")
    return _atomic_write(path, (b"\xef\xbb\xbf" + raw) if meta.get("bom") else raw)


def patch_csv(path, match, updates, add_cols=()):
    """Set `updates` on every row where match(row) is true. Returns number of rows changed."""
    fields, rows, meta = catalog.read_csv(path)
    if fields is None:
        return 0
    hit = [r for r in rows if match(r)]
    if not hit:
        return 0
    for col in add_cols:
        if col not in fields:
            fields.append(col)
    for r in rows:
        for col in add_cols:
            r.setdefault(col, "")
    for r in hit:
        r.update({k: v for k, v in updates.items() if k in fields})
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n" if meta["crlf"] else "\n")
    w.writeheader()
    w.writerows(rows)
    raw = buf.getvalue().encode("utf-8")
    wrote = _atomic_write(path, (b"\xef\xbb\xbf" + raw) if meta["bom"] else raw)
    return len(hit) if wrote else 0


def apply_decision(slug, etype, key, status, reviewer, note=""):
    """Returns the list of files that were rewritten."""
    with LOCK:
        mod = catalog.get_module(slug, fresh=True)   # re-read from disk so we never write stale data
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        note = (note or "").strip()
        stamp = {"review_status": JSON_WORD[status], "reviewed_by": reviewer, "reviewed_at": now,
                 "review_note": note}
        csv_word = CSV_WORD[status]
        csv_upd = {"review_note": note}   # written next to the status in every CSV
        dirty, changed = set(), []   # dirty: JSON paths

        def touch(entry):
            entry.update(stamp)

        if etype == "question":
            for s in mod.sets.values():
                for q in s["data"]["questions"]:
                    if q["code"] == key:
                        touch(q); dirty.add(s["path"])
            for dis in mod.diseases.values():
                for q in dis["data"]["questions"]:
                    if q["code"] == key:
                        touch(q); dirty.add(dis["path"])
            for s in mod.sets.values():   # roll the set-level status up
                qs = s["data"]["questions"]
                if any(q.get("reviewed_at") for q in qs) and s["path"] in dirty:
                    allok = all(catalog.entry_status(q) == "approved" for q in qs)
                    s["data"]["status"] = "doctor_approved" if allok else "doctor_in_review"
            if mod.seed:
                n = patch_csv(mod.seed / "question_bank.csv", lambda r: r.get("question_code") == key,
                              {"source_status": csv_word, **csv_upd}, add_cols=("source_status", "review_note"))
                n2 = patch_csv(mod.seed / "disease_questions.csv", lambda r: r.get("question_code") == key,
                               {"source_status": csv_word, **csv_upd}, add_cols=("source_status", "review_note"))
                changed += [_rel(mod.seed / f) for f, c in (("question_bank.csv", n), ("disease_questions.csv", n2)) if c]

        elif etype == "red_flag":
            dc, _, rf = key.partition("|")
            for r in mod.diseases[dc]["data"].get("red_flags", []):
                if r["code"] == rf:
                    touch(r); dirty.add(mod.diseases[dc]["path"])
            if mod.seed:
                n = patch_csv(mod.seed / "disease_red_flags.csv",
                              lambda r: r.get("disease_code") == dc and r.get("red_flag_code") == rf,
                              {"source_status": csv_word, **csv_upd}, add_cols=("source_status", "review_note"))
                if n:
                    changed.append(_rel(mod.seed / "disease_red_flags.csv"))

        elif etype == "evidence":
            for dis in mod.diseases.values():
                if key in (dis["data"].get("evidence_ids") or []):
                    dis["data"].setdefault("evidence_review", {})[key] = dict(stamp); dirty.add(dis["path"])
            if mod.seed:
                for fn in ("disease_evidence.csv", "evidence_bank.csv"):
                    if patch_csv(mod.seed / fn, lambda r: r.get("code") == key,
                                 {"status": csv_word, **csv_upd}, add_cols=("review_note",)):
                        changed.append(_rel(mod.seed / fn))

        elif etype == "disease":
            d = mod.diseases[key]["data"]
            d["status"] = JSON_WORD[status]; d["reviewed_by"] = reviewer; d["reviewed_at"] = now
            d["review_note"] = note
            dirty.add(mod.diseases[key]["path"])
            if mod.seed:
                n = patch_csv(mod.seed / "diseases.csv", lambda r: r.get("code") == key,
                              {"review_status": csv_word, **csv_upd}, add_cols=("review_status", "review_note"))
                if n:
                    changed.append(_rel(mod.seed / "diseases.csv"))

        for p in sorted(dirty):
            holder = next((x for x in list(mod.diseases.values()) + list(mod.sets.values()) if x["path"] == p))
            if write_json(p, holder["data"], holder["meta"]):
                changed.append(_rel(p))

        v = _update_vectors(mod, etype, key, status, stamp)
        if v:
            changed.append(v)
        catalog.set_cached(slug, mod)
        return changed


# ── field-name mapping: frontend row field → JSON object field ──────────────
_Q_MAP = {"text": "text"}
_RF_MAP = {"condition": "condition"}
_DIS_MAP = {"name": "name", "description": "description", "icd10": "icd10_code"}


def apply_edit(slug, etype, key, fields, reviewer):
    """Overwrite content fields on the matched entity in JSON files, patch CSV
    counterparts, update vector-chunk metadata, and reset review status to
    *pending*.  Returns ``{changed: [...], old_values: {...}, item: {...}}``."""
    with LOCK:
        mod = catalog.get_module(slug, fresh=True)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        dirty, changed = set(), []
        old_values, item = {}, {}

        # ── question ────────────────────────────────────────────────────────
        if etype == "question":
            stamp_q = {"review_status": JSON_WORD["pending"], "reviewed_by": reviewer,
                       "reviewed_at": now, "edited": True, "review_note": ""}
            for s in mod.sets.values():
                for q in s["data"]["questions"]:
                    if q["code"] != key:
                        continue
                    for rf, nv in fields.items():
                        jf = _Q_MAP.get(rf, rf)
                        old_values.setdefault(rf, q.get(jf))
                        q[jf] = nv
                        item[rf] = nv
                    q.update(stamp_q)
                    dirty.add(s["path"])
            for dis in mod.diseases.values():
                for q in dis["data"]["questions"]:
                    if q["code"] != key:
                        continue
                    for rf, nv in fields.items():
                        jf = _Q_MAP.get(rf, rf)
                        old_values.setdefault(rf, q.get(jf))
                        q[jf] = nv
                        item[rf] = nv
                    q.update(stamp_q)
                    dirty.add(dis["path"])
            # roll set-level status
            for s in mod.sets.values():
                if s["path"] in dirty:
                    qs = s["data"]["questions"]
                    allok = all(catalog.entry_status(q) == "approved" for q in qs)
                    s["data"]["status"] = "doctor_approved" if allok else "doctor_in_review"
            # patch CSV
            if mod.seed:
                csv_updates = {k: str(v) for k, v in fields.items()}
                csv_updates["review_note"] = ""   # stale rejection reason
                n = patch_csv(mod.seed / "question_bank.csv",
                              lambda r: r.get("question_code") == key, csv_updates)
                if n:
                    changed.append(_rel(mod.seed / "question_bank.csv"))

        # ── red_flag ────────────────────────────────────────────────────────
        elif etype == "red_flag":
            dc, _, rf = key.partition("|")
            stamp_rf = {"review_status": JSON_WORD["pending"], "reviewed_by": reviewer,
                        "reviewed_at": now, "edited": True, "review_note": ""}
            for r in mod.diseases[dc]["data"].get("red_flags", []):
                if r["code"] != rf:
                    continue
                for rfld, nv in fields.items():
                    if rfld == "condition":
                        old_values.setdefault("condition", r.get("condition"))
                        r["condition"] = nv
                        item["condition"] = nv
                        dirty.add(mod.diseases[dc]["path"])
                r.update(stamp_rf)
                dirty.add(mod.diseases[dc]["path"])
            # action lives in red_flags_reference.json, not the disease file
            if "action" in fields:
                rp = mod.dir / "red_flags_reference.json"
                if rp.exists():
                    rf_data, rf_meta = catalog.read_json(rp)
                    for r in rf_data.get("red_flags", []):
                        if r["code"] == rf:
                            old_values.setdefault("action", r.get("action"))
                            r["action"] = fields["action"]
                            item["action"] = fields["action"]
                    if write_json(rp, rf_data, rf_meta):
                        changed.append(_rel(rp))
            if mod.seed:
                csv_updates = {k: str(v) for k, v in fields.items() if k == "condition"}
                csv_updates["review_note"] = ""
                n = patch_csv(mod.seed / "disease_red_flags.csv",
                              lambda r: r.get("disease_code") == dc and r.get("red_flag_code") == rf,
                              csv_updates)
                if n:
                    changed.append(_rel(mod.seed / "disease_red_flags.csv"))

        # ── disease ──────────────────────────────────────────────────────────
        elif etype == "disease":
            d = mod.diseases[key]["data"]
            for rf, nv in fields.items():
                jf = _DIS_MAP.get(rf, rf)
                old_values.setdefault(rf, d.get(jf))
                d[jf] = nv
                item[rf] = nv
            d["status"] = JSON_WORD["pending"]
            d["reviewed_by"] = reviewer
            d["reviewed_at"] = now
            d["review_note"] = ""
            dirty.add(mod.diseases[key]["path"])
            if mod.seed:
                csv_updates = {_DIS_MAP.get(k, k): str(v) for k, v in fields.items()}
                csv_updates["review_note"] = ""
                n = patch_csv(mod.seed / "diseases.csv",
                              lambda r: r.get("code") == key, csv_updates)
                if n:
                    changed.append(_rel(mod.seed / "diseases.csv"))

        # ── evidence ────────────────────────────────────────────────────────
        elif etype == "evidence":
            for dis in mod.diseases.values():
                if key in (dis["data"].get("evidence_ids") or []):
                    dis["data"].setdefault("evidence_review", {})[key] = {
                        "review_status": JSON_WORD["pending"],
                        "reviewed_by": reviewer, "reviewed_at": now, "edited": True, "review_note": ""}
                    dirty.add(dis["path"])
            if mod.seed:
                csv_updates = {k: str(v) for k, v in fields.items()}
                csv_updates["review_note"] = ""
                for fn in ("disease_evidence.csv", "evidence_bank.csv"):
                    n = patch_csv(mod.seed / fn, lambda r: r.get("code") == key, csv_updates)
                    if n:
                        changed.append(_rel(mod.seed / fn))

        # ── write all dirty JSON ────────────────────────────────────────────
        for p in sorted(dirty):
            holder = next((x for x in list(mod.diseases.values()) + list(mod.sets.values()) if x["path"] == p))
            if write_json(p, holder["data"], holder["meta"]):
                changed.append(_rel(p))

        # ── vector chunks ───────────────────────────────────────────────────
        v = _update_vectors(mod, etype, key, "pending",
                            {"reviewed_by": reviewer, "reviewed_at": now})
        if v:
            changed.append(v)

        catalog.set_cached(slug, mod)
        return {"changed": changed, "old_values": old_values, "item": item}


def _update_vectors(mod, etype, key, status, stamp):
    """vector_chunks_preview.json: add review_status metadata so retrieval can filter on approved."""
    vp = mod.dir / "vector_chunks_preview.json"
    if not vp.exists() or etype not in ("disease", "red_flag"):
        return None
    chunks, meta = catalog.read_json(vp)
    dc = key if etype == "disease" else key.partition("|")[0]
    touched = False
    for ch in chunks:
        if ch.get("disease_code") != dc:
            continue
        if etype == "disease" and ch["section"] in ("overview", "differentials"):
            ch.update({"review_status": status, "reviewed_by": stamp["reviewed_by"], "reviewed_at": stamp["reviewed_at"]})
            touched = True
        elif etype == "red_flag" and ch["section"] == "red_flags":
            rfs = [r for r in mod.diseases[dc]["data"].get("red_flags", []) if not r["code"].startswith("NONE_APPLICABLE")]
            sts = [catalog.entry_status(r) or "pending" for r in rfs]
            roll = "approved" if sts and all(s == "approved" for s in sts) else \
                   "not_approved" if "not_approved" in sts else "pending"
            ch.update({"review_status": roll, "reviewed_by": stamp["reviewed_by"], "reviewed_at": stamp["reviewed_at"]})
            touched = True
    if touched and write_json(vp, chunks, meta):
        return _rel(vp)
    return None