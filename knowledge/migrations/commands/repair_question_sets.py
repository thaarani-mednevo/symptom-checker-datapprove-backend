from django.core.management.base import BaseCommand
from knowledge import catalog, writeback


class Command(BaseCommand):
    help = "Unpack complaint question files whose questions were saved as a list inside `code`."

    def handle(self, *a, **k):
        fixed = 0
        for slug in catalog.module_slugs():
            raw_mod = catalog.get_module(slug, fresh=True)
            for s in raw_mod.sets.values():
                raw, _ = catalog.read_json(s["path"])
                if any(isinstance(q.get("code"), list) for q in raw["questions"]):
                    writeback.write_json(s["path"], s["data"], s["meta"])   # data is already normalised
                    fixed += 1
                    self.stdout.write(f"repaired {s['path'].name}")
        self.stdout.write(self.style.SUCCESS(f"{fixed} files repaired"))