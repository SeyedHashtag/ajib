"""Keep legacy tests and spawned helpers away from installed application state."""
import pytest


@pytest.fixture(autouse=True)
def isolated_default_state(tmp_path, monkeypatch, request):
    # Individual integration fixtures may override these paths. Mocked handler
    # tests still call shared funding services, so their default must be private
    # test storage too, including when the suite runs without root privileges.
    if request.module.__name__ in {'test_crypto_payment_discount', 'test_customer_sales_journey',
                                   'test_reseller_customer_display'}:
        monkeypatch.setenv('AJIB_BOT_DIR', str(tmp_path / 'bot'))
        monkeypatch.setenv('AJIB_DB_PATH', str(tmp_path / 'bot' / 'ajib.db'))
