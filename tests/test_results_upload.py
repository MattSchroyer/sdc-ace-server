import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "results_upload.py"


def load_module():
    spec = importlib.util.spec_from_file_location("results_upload", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_poll_results_directory_moves_uploaded_files_to_results_sent(tmp_path, monkeypatch):
    module = load_module()

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    sent_dir = tmp_path / "results_sent"
    result_path = results_dir / "sample.json"
    result_path.write_text('{"status": "ok"}', encoding="utf-8")

    monkeypatch.setattr(module, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "SENT_RESULTS_DIR", sent_dir)
    monkeypatch.setattr(module, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(module, "_send_result", lambda _url, _path: True)

    def raise_after_first_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", raise_after_first_sleep)

    module._poll_results_directory("https://example.test/webhook")

    assert not result_path.exists()
    assert (sent_dir / "sample.json").exists()
