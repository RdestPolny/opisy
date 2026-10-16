"""Kolejka metatagów na prawdziwym SQLite: batche, wysyłka do Akeneo, kopia zapasowa."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch
import http_clients
import job_store
import metatags
import ui

DESCRIPTION = "Podręcznik do matematyki dla klasy piątej szkoły podstawowej z zadaniami i przykładami. " * 3


def entry(sku, **extra):
    return {"sku": sku, "channel": "Bookland", "locale": "pl_PL", "store_view_code": "sv",
            "product_data": {"title": f"Matematyka {sku}", "author": "Jan Kowalski", "description": DESCRIPTION},
            "run_id": "run-1", **extra}


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "jobs.sqlite3"
        for module in (job_store, ui):
            patcher = patch.object(module, "DB_PATH", db)
            patcher.start()
            self.addCleanup(patcher.stop)
        job_store.init_db()

    def set_status(self, sku, status, batch_name=""):
        with job_store.db_connect() as conn:
            conn.execute("UPDATE meta_jobs SET status=?, batch_job_name=? WHERE sku=?", (status, batch_name, sku))

    def status(self, sku):
        return job_store.get_meta_job(job_store.make_job_key(sku, "Bookland", "pl_PL"))


class BatchQueueTests(QueueTestCase):
    def test_reimport_keeps_jobs_of_running_batch_unless_forced(self):
        job_store.bulk_upsert_meta_jobs([entry("A"), entry("B")])
        self.set_status("A", "batch_submitted", "batches/1")
        changed = entry("A")
        changed["product_data"] = {**changed["product_data"], "description": DESCRIPTION + " Nowe wydanie."}
        result = job_store.bulk_upsert_meta_jobs([changed])
        self.assertEqual(result, {"A": False})
        self.assertEqual(self.status("A")["status"], "batch_submitted")
        job_store.bulk_upsert_meta_jobs([{**changed, "force_regenerate": True}])
        self.assertEqual(self.status("A")["status"], "queued")
        self.assertEqual(self.status("A")["batch_job_name"], "")

    def test_ingest_writes_only_jobs_waiting_for_that_batch_and_fails_missing(self):
        job_store.bulk_upsert_meta_jobs([entry("A"), entry("B"), entry("C")])
        self.set_status("A", "batch_submitted", "batches/1")
        self.set_status("B", "batch_submitted", "batches/1")
        self.set_status("C", "queued")  # zresetowany w międzyczasie
        job_store.register_batch_job(job_name="batches/1", run_id="run-1", display_name="d", input_path=Path("x"),
                                     input_file_name="", product_count=2, state="JOB_STATE_SUCCEEDED")
        answer = json.dumps({"meta_title": "Matematyka A – podręcznik dla klasy 5 szkoły podstawowej",
                             "meta_description": DESCRIPTION[:150]})
        lines = [json.dumps({"key": job_store.make_job_key(sku, "Bookland", "pl_PL"),
                             "response": {"candidates": [{"content": {"parts": [{"text": answer}]}}]}})
                 for sku in ("A", "C")]
        stats = batch.ingest_batch_result_bytes("batches/1", "\n".join(lines).encode("utf-8"))
        self.assertEqual(stats["skipped"], 1)
        self.assertIn(self.status("A")["status"], {"completed", "validation_failed"})
        self.assertEqual(self.status("B")["status"], "failed")
        self.assertIn("Brak odpowiedzi", self.status("B")["error_message"])
        self.assertEqual(self.status("C")["status"], "queued")
        self.assertTrue(job_store.list_batch_jobs()[0]["ingested_at"])

    def test_recovery_ignores_known_and_failed_remote_batches(self):
        job_store.register_batch_job(job_name="batches/old", run_id="", display_name="shard-1", input_path=Path("x"),
                                     input_file_name="", product_count=1, state="JOB_STATE_SUCCEEDED")
        remote = [
            SimpleNamespace(name="batches/old", display_name="shard-1", state=SimpleNamespace(name="JOB_STATE_SUCCEEDED")),
            SimpleNamespace(name="batches/failed", display_name="shard-1", state=SimpleNamespace(name="JOB_STATE_FAILED")),
            SimpleNamespace(name="batches/new", display_name="shard-2", state=SimpleNamespace(name="JOB_STATE_RUNNING")),
        ]
        client = SimpleNamespace(batches=SimpleNamespace(list=lambda config: remote))
        with patch.object(batch.genai, "Client", return_value=client):
            found = batch._remote_batches_by_display_name({"shard-1", "shard-2"}, "key")
        self.assertEqual(set(found), {"shard-2"})
        self.assertEqual(found["shard-2"]["job_name"], "batches/new")


class AkeneoDeliveryTests(QueueTestCase):
    def jobs(self):
        job_store.bulk_upsert_meta_jobs([entry("A"), entry("B"), entry("C")])
        for sku in ("A", "B", "C"):
            job_store.save_meta_results_bulk([{"job_key": job_store.make_job_key(sku, "Bookland", "pl_PL"),
                                               "meta_title": f"Tytuł {sku}", "meta_description": f"Opis {sku}",
                                               "status": "completed"}])
        return job_store.list_meta_delivery_jobs(include_sent=True)

    def test_previous_values_saved_identical_skipped_and_chunk_errors_isolated(self):
        jobs = self.jobs()
        current = {"A": {"meta_title": "Stary A", "meta_description": "Stary opis A"},
                   "B": {"meta_title": "Tytuł B", "meta_description": "Opis B"},
                   "C": {"meta_title": "Stary C", "meta_description": "Stary opis C"}}

        def update_chunk(items, **kwargs):
            if items[0]["sku"] == "C":
                raise RuntimeError("timeout")
            return [(item, "") for item in items]

        with patch.multiple(metatags, akeneo_get_token=lambda: "t", akeneo_get_attribute=lambda *a: {},
                            AKENEO_META_UPDATE_CHUNK_SIZE=1,
                            akeneo_current_metatags=lambda token, skus, channel, locale: {s: current[s] for s in skus},
                            akeneo_update_metatag_chunk=update_chunk,
                            _validated_metatag_texts=lambda title, description: (title, description)):
            stats = metatags.send_metatags_to_akeneo(jobs, "Bookland", "pl_PL")
        self.assertEqual((stats["sent"], stats["unchanged"]), (1, 1))
        self.assertEqual(self.status("A")["akeneo_status"], "sent")
        self.assertEqual(self.status("A")["akeneo_previous_meta_title"], "Stary A")
        self.assertEqual(self.status("B")["akeneo_status"], "sent")
        self.assertEqual(self.status("B")["akeneo_previous_meta_title"], "")
        self.assertEqual(self.status("C")["akeneo_status"], "failed")
        self.assertIn("timeout", self.status("C")["akeneo_error"])
        exported = job_store.export_previous_metatags_csv().decode("utf-8-sig")
        self.assertIn("Stary A", exported)
        self.assertNotIn("Stary C", exported)


class BackupTests(QueueTestCase):
    def test_backup_roundtrip_restores_queue(self):
        job_store.bulk_upsert_meta_jobs([entry("A")])
        archive = ui.build_app_backup()
        self.assertEqual(zipfile.ZipFile(io.BytesIO(archive)).namelist(), ["product_workflow.sqlite3"])
        with job_store.db_connect() as conn:
            conn.execute("DELETE FROM meta_jobs")
        self.assertEqual(ui.restore_app_backup(archive), ["baza zadań i kolejek"])
        self.assertIsNotNone(self.status("A"))

    def test_restore_rejects_foreign_archive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("other.txt", "x")
        with self.assertRaises(ValueError):
            ui.restore_app_backup(buffer.getvalue())


class RetryTests(unittest.TestCase):
    def test_unauthorized_is_retried_once_with_new_token(self):
        calls = []

        class Session:
            def request(self, method, url, timeout=None, **kwargs):
                calls.append(kwargs["headers"]["Authorization"])
                return SimpleNamespace(status_code=401 if len(calls) == 1 else 200, headers={}, text="")

        with patch.object(http_clients, "get_http_session", return_value=Session()):
            response = http_clients.request_with_retry(
                "GET", "https://akeneo.invalid/api/rest/v1/products", headers={"Authorization": "Bearer old"},
                on_unauthorized=lambda stale: f"new-for-{stale}",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["Bearer old", "Bearer new-for-old"])

    def test_server_errors_report_status_without_sleeping_after_last_attempt(self):
        session = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(status_code=503, headers={}, text="busy"))
        with patch.object(http_clients, "get_http_session", return_value=session), \
             patch.object(http_clients.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                http_clients.request_with_retry("GET", "https://x.invalid", max_attempts=2)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
