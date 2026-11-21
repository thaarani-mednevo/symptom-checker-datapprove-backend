import json

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import catalog, writeback
from .models import ReviewDecision, ReviewEvent


def _mod_or_404(slug):
    return catalog.get_module(slug)


@require_GET
def modules(request):
    out = []
    for slug in catalog.module_slugs():
        m = catalog.get_module(slug)
        out.append({"slug": slug, "name": catalog.pretty(slug.replace("-", " ")),
                    "diseases": len(m.diseases), "csv_only_diseases": m.csv_only_diseases,
                    "complaint_sets": len(m.sets), "progress": m.progress(catalog.ledger_for(slug))})
    return JsonResponse({"modules": out})


@require_GET
def symptoms(request, slug):
    m = _mod_or_404(slug)
    if not m:
        return JsonResponse({"error": "unknown module"}, status=404)
    return JsonResponse({"module": slug, "symptoms": m.symptoms(request.GET.get("q", ""))})


@require_GET
def symptom_detail(request, slug, kind, code):
    m = _mod_or_404(slug)
    if not m or kind not in ("complaint", "symptom"):
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(m.detail(kind, code, catalog.ledger_for(slug)))


def _decide(slug, etype, key, status, reviewer, note):
    m = catalog.get_module(slug)
    if not m or etype not in catalog.ENTITY_TYPES or status not in catalog.STATUSES:
        return {"key": key, "ok": False, "error": "bad module / entity_type / status"}
    if not m.has_key(etype, key):
        return {"key": key, "ok": False, "error": "item not found in module"}
    old = ReviewDecision.objects.filter(module=slug, entity_type=etype, key=key).values_list("status", flat=True).first()
    files = writeback.apply_decision(slug, etype, key, status, reviewer, note)   # files first; ledger only if that worked
    with transaction.atomic():
        ReviewDecision.objects.update_or_create(module=slug, entity_type=etype, key=key,
                                                defaults={"status": status, "reviewer": reviewer, "note": note})
        ReviewEvent.objects.create(module=slug, entity_type=etype, key=key, old_status=old or "",
                                   new_status=status, reviewer=reviewer, note=note, files_changed=files)
    return {"key": key, "ok": True, "status": status, "files_changed": files}


def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


@csrf_exempt   # dev only. Put real doctor login in front of this before production.
@require_POST
def review(request):
    b = _body(request)
    if not b or not str(b.get("reviewer", "")).strip():
        return JsonResponse({"error": "reviewer is required"}, status=400)
    r = _decide(b.get("module"), b.get("entity_type"), b.get("key"), b.get("status"), b["reviewer"].strip(), b.get("note", ""))
    return JsonResponse(r, status=200 if r["ok"] else 400)


@csrf_exempt
@require_POST
def review_bulk(request):
    b = _body(request)
    if not b or not str(b.get("reviewer", "")).strip():
        return JsonResponse({"error": "reviewer is required"}, status=400)
    res = [_decide(b.get("module"), i.get("entity_type"), i.get("key"), b.get("status"), b["reviewer"].strip(), b.get("note", ""))
           for i in b.get("items", [])]
    return JsonResponse({"results": res, "failed": sum(1 for r in res if not r["ok"])})


@csrf_exempt
@require_POST
def review_edit(request):
    """Doctor edits content fields on an item.  Saves to JSON files + CSV +
    vector chunks, resets review status to *pending*, and logs a ReviewEvent."""
    b = _body(request)
    if not b or not str(b.get("reviewer", "")).strip():
        return JsonResponse({"error": "reviewer is required"}, status=400)
    slug   = b.get("module")
    etype  = b.get("entity_type")
    key    = b.get("key")
    fields = b.get("fields")
    reviewer = b["reviewer"].strip()

    # ── validate ──────────────────────────────────────────────────────────
    m = catalog.get_module(slug)
    if not m:
        return JsonResponse({"error": "unknown module"}, status=400)
    if etype not in catalog.ENTITY_TYPES:
        return JsonResponse({"error": "bad entity_type"}, status=400)
    if not key or not isinstance(fields, dict) or not fields:
        return JsonResponse({"error": "key and non-empty fields are required"}, status=400)
    if not m.has_key(etype, key):
        return JsonResponse({"error": "item not found in module"}, status=400)

    # ── call writeback ────────────────────────────────────────────────────
    try:
        result = writeback.apply_edit(slug, etype, key, fields, reviewer)
    except ValueError as exc:   # bad input: nothing was written
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"write failed: {exc}"}, status=500)

    changed    = result["changed"]
    old_values = result["old_values"]
    item       = result["item"]

    # ── persist to DB (review status reset to pending, audit event) ────────
    note_parts = [f"{f}: {repr(old_values.get(f))[:60]} -> {repr(fields[f])[:60]}" for f in fields]
    note = "Edited " + "; ".join(note_parts)
    old = ReviewDecision.objects.filter(module=slug, entity_type=etype, key=key).values_list("status", flat=True).first()
    with transaction.atomic():
        ReviewDecision.objects.update_or_create(
            module=slug, entity_type=etype, key=key,
            defaults={"status": "pending", "reviewer": reviewer, "note": note})
        ReviewEvent.objects.create(
            module=slug, entity_type=etype, key=key,
            old_status=old or "", new_status="pending",
            reviewer=reviewer, note=note, files_changed=changed)

    return JsonResponse({"ok": True, "item": item, "files_changed": changed}, status=200)


@require_GET
def review_log(request):
    qs = ReviewEvent.objects.all()
    if request.GET.get("module"):
        qs = qs.filter(module=request.GET["module"])
    return JsonResponse({"events": [{"module": e.module, "entity_type": e.entity_type, "key": e.key,
                                     "old": e.old_status, "new": e.new_status, "reviewer": e.reviewer,
                                     "note": e.note, "files": e.files_changed, "at": e.at.isoformat()} for e in qs[:200]]})