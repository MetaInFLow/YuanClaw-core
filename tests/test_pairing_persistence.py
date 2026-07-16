import json

import pytest

from yuanclaw.pairing import approve_code, generate_code, get_approved
from yuanclaw.pairing.store import PairingStoreCorruptError


def test_pairing_store_recovers_previous_atomic_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.pairing.store.get_data_dir", lambda: tmp_path)
    first_code = generate_code("signal", "first")
    assert approve_code(first_code) == ("signal", "first")
    second_code = generate_code("signal", "second")
    assert approve_code(second_code) == ("signal", "second")
    path = tmp_path / "pairing.json"
    path.write_text("{broken", encoding="utf-8")

    approved = get_approved("signal")

    assert approved == ["first"]
    assert list(tmp_path.glob("pairing.json.corrupt-*"))


def test_pairing_store_damage_without_backup_is_explicit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.pairing.store.get_data_dir", lambda: tmp_path)
    path = tmp_path / "pairing.json"
    path.write_text(json.dumps({"approved": [], "pending": {}}), encoding="utf-8")

    with pytest.raises(PairingStoreCorruptError, match="Invalid pairing store"):
        get_approved("signal")

    assert path.is_file()
