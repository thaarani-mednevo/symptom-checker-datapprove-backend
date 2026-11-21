import io
import zipfile
from datetime import datetime, timezone

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET

from . import catalog, writeback

MAX_VIEW_BYTES = 3_000_000
MAX_CSV_ROWS = 500


def _files(slug):
    mod = catalog.get_module(slug)
    if not mod:
        return None, None
    out = {"json": {}, "csv": {}}
    for p in sorted(mod.dir.rglob("*.json")):
        out["json"][p.relative_to(mod.dir).as_posix()] = p
    if mod.seed:
        for p in sorted(mod.seed.glob("*.csv")):
            out["csv"][p.name] = p
    return mod, out


def _pick(request, slug):
    mod, f = _files(slug)
    if not f:
        return None, None, None
    kind = request.GET.get("kind", "")
    rel = request.GET.get("path", "")
    return kind, rel, (f.get(kind) or {}).get(rel)


@require_GET
def file_list(request, slug):
    mod, f = _files(slug)
    if not f:
        return JsonResponse({"error": "unknown module"}, status=404)

    def info(kind):
        rows = []
        for rel, p in f[kind].items():
            st = p.stat()
            rows.append({
                "path": rel, "name": p.name,
                "group": rel.split("/")[0] if "/" in rel else "(module root)",
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
        return rows

    return JsonResponse({"module": slug, "json": info("json"), "csv": info("csv")})


@require_GET
def file_view(request, slug):
    kind, rel, p = _pick(request, slug)
    if not p:
        return JsonResponse({"error": "file not found"}, status=404)
    raw = p.read_bytes()
    if kind == "json":
        text = raw.decode("utf-8-sig")
        return JsonResponse({"kind": "json", "path": rel, "size": len(raw),
                             "truncated": len(text) > MAX_VIEW_BYTES, "text": text[:MAX_VIEW_BYTES]})
    fields, rows, _ = catalog.read_csv(p)
    return JsonResponse({"kind": "csv", "path": rel, "size": len(raw), "fields": fields or [],
                         "total": len(rows), "rows": rows[:MAX_CSV_ROWS]})


@require_GET
def file_download(request, slug):
    kind, rel, p = _pick(request, slug)
    if not p:
        return JsonResponse({"error": "file not found"}, status=404)
    ctype = "application/json" if kind == "json" else "text/csv; charset=utf-8"
    return FileResponse(open(p, "rb"), as_attachment=True, filename=p.name, content_type=ctype)


@require_GET
def module_download(request, slug):
    fmt = request.GET.get("format", "all")
    if fmt not in ("json", "csv", "all"):
        return JsonResponse({"error": "format must be json, csv or all"}, status=400)
    mod, f = _files(slug)
    if not f:
        return JsonResponse({"error": "unknown module"}, status=404)
    buf = io.BytesIO()
    with writeback.LOCK:   # no edit can land half-way through the zip
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if fmt in ("json", "all"):
                for rel, p in f["json"].items():
                    z.write(p, f"data/{slug}/{rel}")
            if fmt in ("csv", "all") and mod.seed:
                for rel, p in f["csv"].items():
                    z.write(p, f"seed_data/{mod.seed.name}/{rel}")
    buf.seek(0)
    return FileResponse(buf, as_attachment=True, filename=f"{slug}_{fmt}.zip", content_type="application/zip")