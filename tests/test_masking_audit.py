from app import audit
from app.db import connect
from app.masking import find_leaks, mask


def test_patient_name_phone_email_are_masked():
    result = mask("Пацієнтка Олена Петренко, тел. +380 67 123 45 67, пише на olena@example.com")
    assert "Петренко" not in result.text
    assert "67 123" not in result.text
    assert "olena@example.com" not in result.text
    assert "[ПАЦІЄНТ_1]" in result.text
    assert result.mapping["[ПАЦІЄНТ_1]"] == "Олена Петренко"


def test_date_of_birth_is_masked_but_billing_date_is_kept():
    result = mask("Пацієнт 1987 р.н., дата народження 03.04.1987. Списали двічі 30.10.2026")
    assert "03.04.1987" not in result.text
    assert "30.10.2026" in result.text


def test_medical_record_number_is_masked():
    assert "№ 48213" not in mask("Карта № 48213").text


def test_plain_complaint_is_untouched():
    text = "Третій раз AI пише «гіперехогенне» замість «гіпоехогенне»!!!"
    assert mask(text).text == text


def test_public_reply_with_patient_data_is_caught():
    assert find_leaks("Ми перевірили протокол пацієнтки Олени Петренко") != []
    assert find_leaks("Зателефонуйте нам: +380 67 123 45 67") != []
    assert find_leaks("Деталі в кейсі [ПАЦІЄНТ_1]") != []


def test_clean_public_reply_passes():
    assert find_leaks("Дякуємо за відгук! Ми знайшли проблему з розпізнаванням терміна і вже виправили її.") == []


def test_audit_chain_detects_tampering():
    conn = connect(":memory:")
    for i in range(3):
        audit.log(conn, "action_executed", {"n": i}, case_id=1)
    assert audit.verify(conn)["ok"]
    conn.execute("UPDATE audit SET data = ? WHERE id = 2", ('{"n": 999}',))
    status = audit.verify(conn)
    assert not status["ok"] and status["broken_at"] == 2
