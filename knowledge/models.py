from django.db import models


class ReviewDecision(models.Model):
    """Current doctor decision for one item. Source of truth for status + audit."""
    STATUSES = [("approved", "Approved"), ("not_approved", "Not approved"), ("pending", "Pending")]
    TYPES = [("question", "Question"), ("red_flag", "Red flag"), ("evidence", "Evidence link"), ("disease", "Disease")]

    module = models.CharField(max_length=60)
    entity_type = models.CharField(max_length=20, choices=TYPES)
    key = models.CharField(max_length=300)
    status = models.CharField(max_length=20, choices=STATUSES)
    reviewer = models.CharField(max_length=120)
    note = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("module", "entity_type", "key")]
        indexes = [models.Index(fields=["module", "entity_type"])]

    def __str__(self):
        return f"{self.module}/{self.entity_type}/{self.key} -> {self.status}"


class ReviewEvent(models.Model):
    """Append-only history. Every click is kept here."""
    module = models.CharField(max_length=60)
    entity_type = models.CharField(max_length=20)
    key = models.CharField(max_length=300)
    old_status = models.CharField(max_length=20, blank=True, default="")
    new_status = models.CharField(max_length=20)
    reviewer = models.CharField(max_length=120)
    note = models.TextField(blank=True, default="")
    files_changed = models.JSONField(default=list)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]