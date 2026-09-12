"""Durable web fulfilment and notification worker; no Telegram polling."""
import logging
import os
import time


def run_once(services, *, writes_enabled=True):
    from utils import database, web_store
    from utils.web_orders import Orders, save_payment
    if not writes_enabled:
        return False
    now = int(time.time())
    # A process may have exited after sending an external request. Never replay it.
    with database.transaction(operation="web_worker_recover") as connection:
        rows = list(connection.execute("SELECT id,scope,status FROM web_operations WHERE status IN ('processing','creating') AND updated_at<?", (now - 600,)))
        for row in rows:
            connection.execute("UPDATE web_operations SET status='uncertain',updated_at=? WHERE id=?", (now, row["id"]))
            reason = 'gateway_creation_interrupted' if row['status'] == 'creating' else 'worker_interrupted'
            save_payment(connection, row["scope"], row["id"], {"status": "uncertain", "web_attention_reason": reason})
    # Gateway status is obtained from the existing signed server-side client.
    from utils.payments import CryptoPayment
    from utils.web_gateway import poll
    poll(services, CryptoPayment())
    worked = Orders(services).process_one()
    from utils.web_trials import process_one as process_trial
    worked = process_trial(services) or worked
    item = web_store.claim_notification()
    if item:
        try:
            import requests
            token = services.bot_token(item["scope"])
            if not token:
                raise RuntimeError("Bot token unavailable")
            response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": item["recipient"], "text": item["text"]}, timeout=20)
            if response.status_code != 200 or not response.json().get("ok"):
                raise RuntimeError("Telegram delivery rejected")
        except Exception as error:
            # Do not log exception messages: requests errors can contain bot tokens.
            web_store.finish_notification(item, type(error).__name__)
        else:
            web_store.finish_notification(item)
        worked = True
    return worked


def main():
    os.environ["AJIB_BOT_ROLE"] = "web-worker"
    from .runtime import configure
    from .settings import Settings
    configure()
    from utils import database, web_store, web_release
    from utils.web_services import Services
    if os.getenv("AJIB_SQLITE_ACTIVE") != "1":
        raise RuntimeError("Migrate bot storage before starting the web worker")
    web_store.initialize()
    logging.basicConfig(level=logging.INFO)
    services = Services()
    while True:
        writes_enabled = False
        try:
            writes_enabled = web_release.policy(Settings.from_env())['process_existing']
            run_once(services, writes_enabled=writes_enabled)
        except Exception as error:
            logging.getLogger("ajib.web.worker").error("worker_iteration_failed type=%s", type(error).__name__)
            last_error = type(error).__name__
        else:
            last_error = None
        try:
            with database.transaction(operation="web_worker_health") as connection:
                connection.execute("""INSERT INTO web_worker_health VALUES ('worker',?,?,?,?)
                    ON CONFLICT(role) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,
                    last_success_at=COALESCE(excluded.last_success_at,web_worker_health.last_success_at),
                    last_error=excluded.last_error,writes_enabled=excluded.writes_enabled""",
                    (int(time.time()), None if last_error else int(time.time()), last_error, int(writes_enabled)))
        except Exception as error:
            logging.getLogger("ajib.web.worker").error("worker_health_failed type=%s", type(error).__name__)
        time.sleep(5)


if __name__ == "__main__":
    main()
